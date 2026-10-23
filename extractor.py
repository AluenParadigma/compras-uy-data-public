import csv
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


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

CONNECT_TIMEOUT = 30
READ_TIMEOUT = 120

MAX_WINDOW_ATTEMPTS = 5

BASE_RETRY_SECONDS = 8

# ARCE puede devolver campos muy grandes en XML/CSV.
csv.field_size_limit(sys.maxsize)


# ============================================================
# SESION HTTP
# ============================================================

def create_session():
    session = requests.Session()

    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=2,
        status_forcelist=[
            429,
            500,
            502,
            503,
            504,
        ],
        allowed_methods=["GET"],
        raise_on_status=False,
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=10,
        pool_maxsize=10,
    )

    session.mount(
        "https://",
        adapter,
    )

    session.mount(
        "http://",
        adapter,
    )

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
            "Accept-Language": (
                "es-UY,es;q=0.9,en;q=0.8"
            ),
            "Connection": "keep-alive",
        }
    )

    return session


# ============================================================
# PARAMETROS
# ============================================================

def build_params(
    start_date,
    end_date,
    publication_type,
):
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

    # --------------------------------------------------------
    # ATRIBUTOS XML
    # --------------------------------------------------------

    for key, value in element.attrib.items():
        result[
            normalize_tag(key)
        ] = value

    # --------------------------------------------------------
    # TEXTO
    # --------------------------------------------------------

    text = (
        element.text or ""
    ).strip()

    if text:
        result["_text"] = text

    # --------------------------------------------------------
    # HIJOS
    # --------------------------------------------------------

    for child in list(element):
        key = normalize_tag(
            child.tag
        )

        child_value = (
            xml_element_to_dict(
                child
            )
        )

        if not child_value:
            child_value = None

        if key in result:
            if not isinstance(
                result[key],
                list,
            ):
                result[key] = [
                    result[key]
                ]

            result[key].append(
                child_value
            )

        else:
            result[key] = (
                child_value
            )

    return result


def find_compra_nodes(root):
    compras = []

    for element in root.iter():
        tag = normalize_tag(
            element.tag
        ).lower()

        if tag == "compra":
            compras.append(
                element
            )

    return compras


# ============================================================
# FLATTEN
# ============================================================

def flatten(
    data,
    parent_key="",
    separator="_",
):
    items = {}

    if isinstance(data, dict):

        for key, value in data.items():

            new_key = (
                f"{parent_key}"
                f"{separator}"
                f"{key}"
                if parent_key
                else key
            )

            if isinstance(
                value,
                dict,
            ):
                items.update(
                    flatten(
                        value,
                        new_key,
                        separator,
                    )
                )

            elif isinstance(
                value,
                list,
            ):
                items[
                    new_key
                ] = json.dumps(
                    value,
                    ensure_ascii=False,
                )

            elif value is None:
                items[new_key] = ""

            else:
                items[
                    new_key
                ] = value

    else:
        items[
            parent_key
        ] = data

    return items


# ============================================================
# CALIDAD
# ============================================================

def count_non_empty_scalar_fields(
    record,
):
    flattened = flatten(
        record
    )

    count = 0

    for value in (
        flattened.values()
    ):

        if value is None:
            continue

        text = str(
            value
        ).strip()

        if not text:
            continue

        if text in (
            "[]",
            "{}",
            "null",
        ):
            continue

        count += 1

    return count


def record_has_real_content(
    record,
):
    return (
        count_non_empty_scalar_fields(
            record
        )
        >= 3
    )


# ============================================================
# IDENTIFICADOR
# ============================================================

def make_record_key(record):
    flattened = {
        str(k).lower():
        str(v).strip()
        for k, v
        in flatten(record).items()
        if v not in (
            None,
            "",
        )
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

    # --------------------------------------------------------
    # IDENTIFICADOR DIRECTO
    # --------------------------------------------------------

    for candidate in (
        id_candidates
    ):
        if candidate in flattened:
            return (
                f"{candidate}:"
                f"{flattened[candidate]}"
            )

    # --------------------------------------------------------
    # IDENTIFICADOR ANIDADO
    # --------------------------------------------------------

    for candidate in (
        id_candidates
    ):

        for key, value in (
            flattened.items()
        ):

            if (
                key.endswith(
                    candidate
                )
                and value
            ):
                return (
                    f"{candidate}:"
                    f"{value}"
                )

    # --------------------------------------------------------
    # FALLBACK COMPUESTO
    # --------------------------------------------------------

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

        for key, value in (
            flattened.items()
        ):

            if (
                keyword in key
                and value
            ):
                fallback_values.append(
                    f"{keyword}:"
                    f"{value}"
                )
                break

    if len(
        fallback_values
    ) >= 2:

        return "|".join(
            fallback_values
        )

    return json.dumps(
        record,
        sort_keys=True,
        ensure_ascii=False,
    )


# ============================================================
# VENTANAS
# ============================================================

def generate_windows(
    start_date,
    end_date,
):
    current = (
        start_date
    )

    while (
        current
        <= end_date
    ):

        window_end = min(
            current
            + timedelta(
                days=(
                    WINDOW_DAYS
                    - 1
                )
            ),
            end_date,
        )

        yield (
            current,
            window_end,
        )

        current = (
            window_end
            + timedelta(
                days=1
            )
        )


# ============================================================
# RETRY
# ============================================================

def retry_wait_seconds(
    attempt,
):
    """
    Backoff exponencial con jitter.

    Intentos:
    1 -> ~8 s
    2 -> ~16 s
    3 -> ~32 s
    4 -> ~64 s
    5 -> ~128 s
    """

    base = (
        BASE_RETRY_SECONDS
        * (
            2
            ** (
                attempt
                - 1
            )
        )
    )

    jitter = random.uniform(
        0,
        3,
    )

    return (
        base
        + jitter
    )


# ============================================================
# DESCARGA DE UNA VENTANA
# ============================================================

def download_window_once(
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

    # --------------------------------------------------------
    # ERRORES TRANSITORIOS
    # --------------------------------------------------------

    if response.status_code in (
        403,
        429,
        500,
        502,
        503,
        504,
    ):

        raise RuntimeError(
            "HTTP_TRANSIENT_"
            f"{response.status_code}"
        )

    response.raise_for_status()

    if not response.content:
        raise RuntimeError(
            "EMPTY_RESPONSE"
        )

    # --------------------------------------------------------
    # XML
    # --------------------------------------------------------

    try:
        root = ET.fromstring(
            response.content
        )

    except ET.ParseError as exc:

        preview = (
            response.text[:500]
        )

        raise RuntimeError(
            "INVALID_XML: "
            f"{preview}"
        ) from exc

    compra_nodes = (
        find_compra_nodes(
            root
        )
    )

    records = []

    for node in (
        compra_nodes
    ):

        record = (
            xml_element_to_dict(
                node
            )
        )

        if not record:
            continue

        records.append(
            record
        )

    return records


def download_window(
    session,
    start_date,
    end_date,
    publication_type,
):
    label = (
        "LLAMADOS VIGENTES"
        if publication_type
        == "lv"
        else "TODOS LOS LLAMADOS"
    )

    print(
        f"[{label}] "
        f"{start_date:%d/%m/%Y}"
        f" - "
        f"{end_date:%d/%m/%Y}"
    )

    last_error = None

    for attempt in range(
        1,
        MAX_WINDOW_ATTEMPTS
        + 1,
    ):

        try:

            records = (
                download_window_once(
                    session,
                    start_date,
                    end_date,
                    publication_type,
                )
            )

            print(
                "  compras encontradas:",
                len(records),
                f"(intento {attempt})",
            )

            return records

        except (
            requests.Timeout,
            requests.ConnectionError,
            RuntimeError,
            requests.HTTPError,
        ) as exc:

            last_error = exc

            print(
                f"  intento "
                f"{attempt}/"
                f"{MAX_WINDOW_ATTEMPTS}"
                f" fallo: "
                f"{exc}",
                file=sys.stderr,
            )

            if (
                attempt
                >= MAX_WINDOW_ATTEMPTS
            ):
                break

            wait_seconds = (
                retry_wait_seconds(
                    attempt
                )
            )

            print(
                "  esperando "
                f"{wait_seconds:.1f}s "
                "antes de reintentar..."
            )

            time.sleep(
                wait_seconds
            )

    raise RuntimeError(
        "WINDOW_FAILED_AFTER_RETRIES: "
        f"{last_error}"
    )


# ============================================================
# EXTRACCION COMPLETA
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
    failed_windows = 0

    failed_window_details = []

    for start, end in (
        generate_windows(
            start_date,
            end_date,
        )
    ):

        try:

            records = (
                download_window(
                    session,
                    start,
                    end,
                    publication_type,
                )
            )

            all_records.extend(
                records
            )

            windows_processed += 1

            if records:
                windows_with_records += 1

        except Exception as exc:

            failed_windows += 1

            detail = {
                "publication_type":
                    publication_type,
                "start":
                    start.strftime(
                        "%Y-%m-%d"
                    ),
                "end":
                    end.strftime(
                        "%Y-%m-%d"
                    ),
                "error":
                    str(exc),
            }

            failed_window_details.append(
                detail
            )

            print(
                "ERROR DEFINITIVO "
                f"{publication_type} "
                f"{start:%d/%m/%Y}"
                " - "
                f"{end:%d/%m/%Y}: "
                f"{exc}",
                file=sys.stderr,
            )

    # --------------------------------------------------------
    # DEDUPLICACION
    # --------------------------------------------------------

    unique = {}

    for record in all_records:

        key = (
            make_record_key(
                record
            )
        )

        unique[key] = (
            record
        )

    # --------------------------------------------------------
    # CALIDAD
    # --------------------------------------------------------

    records_with_real_content = 0

    for record in (
        unique.values()
    ):

        if (
            record_has_real_content(
                record
            )
        ):
            records_with_real_content += 1

    return {
        "raw_records":
            all_records,
        "unique":
            unique,
        "windows_processed":
            windows_processed,
        "windows_with_records":
            windows_with_records,
        "failed_windows":
            failed_windows,
        "failed_window_details":
            failed_window_details,
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
        lv_keys
        - all_keys
    )

    common = (
        lv_keys
        & all_keys
    )

    return {
        "common":
            common,
        "missing_in_all":
            missing_in_all,
        "lv_is_subset_of_all":
            (
                len(
                    missing_in_all
                )
                == 0
            ),
    }


# ============================================================
# JSON TEMPORAL
# ============================================================

def write_json_temp(
    records,
):
    payload = {
        "generated_at":
            datetime.now(
                timezone.utc
            ).isoformat(),
        "source":
            (
                "Compras Estatales "
                "Uruguay / ARCE"
            ),
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


# ============================================================
# CSV TEMPORAL
# ============================================================

def write_csv_temp(
    records,
):
    flat_records = [
        flatten(record)
        for record
        in records
    ]

    if not flat_records:

        TEMP_CSV.write_text(
            "sin_registros\n",
            encoding="utf-8",
        )

        return

    columns = sorted(
        {
            key
            for record
            in flat_records
            for key
            in record.keys()
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

        for record in (
            flat_records
        ):
            writer.writerow(
                record
            )


def count_temp_csv_records():
    if not TEMP_CSV.exists():
        return 0

    with TEMP_CSV.open(
        "r",
        encoding="utf-8-sig",
        newline="",
    ) as file:

        reader = (
            csv.reader(file)
        )

        count = -1

        for _ in reader:
            count += 1

    return max(
        count,
        0,
    )


# ============================================================
# METADATA TEMPORAL
# ============================================================

def build_metadata(
    lv_result,
    all_result,
    cross_result,
):
    lv_unique_records = len(
        lv_result[
            "unique"
        ]
    )

    all_unique_records = len(
        all_result[
            "unique"
        ]
    )

    json_records = (
        lv_unique_records
    )

    csv_records = (
        count_temp_csv_records()
    )

    lv_content_records = (
        lv_result[
            "records_with_real_content"
        ]
    )

    all_content_records = (
        all_result[
            "records_with_real_content"
        ]
    )

    lv_xml_complete = (
        lv_result[
            "failed_windows"
        ]
        == 0
        and
        lv_unique_records
        > 0
    )

    all_xml_complete = (
        all_result[
            "failed_windows"
        ]
        == 0
        and
        all_unique_records
        > 0
    )

    file_integrity = (
        json_records
        == csv_records
        == lv_unique_records
    )

    cross_validation_match = (
        cross_result[
            "lv_is_subset_of_all"
        ]
    )

    content_quality_complete = (
        lv_unique_records
        > 0
        and
        lv_content_records
        == lv_unique_records
    )

    xml_double_validation = (
        lv_xml_complete
        and
        all_xml_complete
        and
        file_integrity
        and
        cross_validation_match
    )

    coverage_complete = (
        xml_double_validation
        and
        content_quality_complete
    )

    if coverage_complete:

        validation_status = "OK"

        note = (
            "Cobertura XML validada por "
            "doble extraccion oficial ARCE. "
            "Todas las ventanas fueron "
            "procesadas sin error, los "
            "llamados vigentes estan "
            "contenidos en el universo "
            "general, JSON y CSV coinciden "
            "y todos los registros contienen "
            "informacion sustantiva."
        )

    else:

        validation_status = "ERROR"

        note = (
            "La extraccion presenta "
            "ventanas fallidas, problemas "
            "de integridad, validacion "
            "cruzada o calidad de contenido. "
            "El snapshot anterior se conserva."
        )

    return {
        "extraction_timestamp":
            datetime.now(
                timezone.utc
            ).isoformat(),

        "source":
            (
                "Compras Estatales "
                "Uruguay / ARCE"
            ),

        "source_endpoint":
            BASE_URL,

        "start_date":
            START_DATE.strftime(
                "%Y-%m-%d"
            ),

        "max_window_attempts":
            MAX_WINDOW_ATTEMPTS,

        # LV
        "lv_windows_processed":
            lv_result[
                "windows_processed"
            ],

        "lv_windows_with_records":
            lv_result[
                "windows_with_records"
            ],

        "lv_failed_windows":
            lv_result[
                "failed_windows"
            ],

        "lv_failed_window_details":
            lv_result[
                "failed_window_details"
            ],

        "lv_raw_records":
            len(
                lv_result[
                    "raw_records"
                ]
            ),

        "lv_unique_records":
            lv_unique_records,

        "lv_records_with_real_content":
            lv_content_records,

        # ALL
        "all_windows_processed":
            all_result[
                "windows_processed"
            ],

        "all_windows_with_records":
            all_result[
                "windows_with_records"
            ],

        "all_failed_windows":
            all_result[
                "failed_windows"
            ],

        "all_failed_window_details":
            all_result[
                "failed_window_details"
            ],

        "all_raw_records":
            len(
                all_result[
                    "raw_records"
                ]
            ),

        "all_unique_records":
            all_unique_records,

        "all_records_with_real_content":
            all_content_records,

        # CROSS
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

        # FILES
        "json_records":
            json_records,

        "csv_records":
            csv_records,

        "file_integrity":
            file_integrity,

        # VALIDATION
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
            validation_status,

        "note":
            note,
    }


def write_metadata_temp(
    metadata,
):
    TEMP_METADATA.write_text(
        json.dumps(
            metadata,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ============================================================
# PUBLICAR SNAPSHOT
# ============================================================

def publish_snapshot():
    """
    Solo se llama si TODAS las validaciones
    fueron exitosas.
    """

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
# LIMPIAR TEMPORALES
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


# ============================================================
# MAIN
# ============================================================

def main():
    cleanup_temp_files()

    today = (
        datetime.now().date()
    )

    end_date = datetime(
        today.year,
        today.month,
        today.day,
    )

    session = (
        create_session()
    )

    # ========================================================
    # A - LLAMADOS VIGENTES
    # ========================================================

    print("")
    print(
        "===================================="
    )
    print(
        "EXTRACCION A - LLAMADOS VIGENTES"
    )
    print(
        "===================================="
    )

    lv_result = (
        extract_publication_type(
            session=session,
            publication_type="lv",
            start_date=START_DATE,
            end_date=end_date,
        )
    )

    print("")
    print(
        "LV crudos:",
        len(
            lv_result[
                "raw_records"
            ]
        ),
    )

    print(
        "LV unicos:",
        len(
            lv_result[
                "unique"
            ]
        ),
    )

    print(
        "LV fallidas:",
        lv_result[
            "failed_windows"
        ],
    )

    # Si LV falla, no tiene sentido continuar.
    if (
        lv_result[
            "failed_windows"
        ]
        > 0
    ):

        print("")
        print(
            "ERROR: hubo ventanas "
            "fallidas en llamados vigentes."
        )

        print(
            "Se conserva el ultimo "
            "snapshot valido."
        )

        cleanup_temp_files()

        sys.exit(1)

    # ========================================================
    # B - TODOS LOS LLAMADOS
    # ========================================================

    print("")
    print(
        "===================================="
    )
    print(
        "EXTRACCION B - TODOS LOS LLAMADOS"
    )
    print(
        "===================================="
    )

    all_result = (
        extract_publication_type(
            session=session,
            publication_type="l",
            start_date=START_DATE,
            end_date=end_date,
        )
    )

    print("")
    print(
        "ALL crudos:",
        len(
            all_result[
                "raw_records"
            ]
        ),
    )

    print(
        "ALL unicos:",
        len(
            all_result[
                "unique"
            ]
        ),
    )

    print(
        "ALL fallidas:",
        all_result[
            "failed_windows"
        ],
    )

    if (
        all_result[
            "failed_windows"
        ]
        > 0
    ):

        print("")
        print(
            "ERROR: hubo ventanas "
            "fallidas en todos los llamados."
        )

        print(
            "Se conserva el ultimo "
            "snapshot valido."
        )

        cleanup_temp_files()

        sys.exit(1)

    # ========================================================
    # VALIDACION CRUZADA
    # ========================================================

    cross_result = (
        cross_validate(
            lv_result[
                "unique"
            ],
            all_result[
                "unique"
            ],
        )
    )

    print("")
    print(
        "===================================="
    )
    print(
        "VALIDACION CRUZADA"
    )
    print(
        "===================================="
    )

    print(
        "LV encontrados en ALL:",
        len(
            cross_result[
                "common"
            ]
        ),
    )

    print(
        "LV faltantes en ALL:",
        len(
            cross_result[
                "missing_in_all"
            ]
        ),
    )

    if not (
        cross_result[
            "lv_is_subset_of_all"
        ]
    ):

        print(
            "ERROR: existen llamados "
            "vigentes que no aparecen "
            "en todos los llamados."
        )

        cleanup_temp_files()

        sys.exit(1)

    # ========================================================
    # ARCHIVOS TEMPORALES
    # ========================================================

    records = list(
        lv_result[
            "unique"
        ].values()
    )

    if not records:

        print(
            "ERROR: no se obtuvieron "
            "llamados vigentes."
        )

        cleanup_temp_files()

        sys.exit(1)

    write_json_temp(
        records
    )

    write_csv_temp(
        records
    )

    metadata = (
        build_metadata(
            lv_result,
            all_result,
            cross_result,
        )
    )

    write_metadata_temp(
        metadata
    )

    # ========================================================
    # VALIDACION FINAL
    # ========================================================

    if not (
        metadata[
            "coverage_complete"
        ]
    ):

        print(
            "ERROR: la validacion "
            "final no fue satisfactoria."
        )

        print(
            json.dumps(
                metadata,
                ensure_ascii=False,
                indent=2,
            )
        )

        cleanup_temp_files()

        sys.exit(1)

    # ========================================================
    # PUBLICAR
    # ========================================================

    publish_snapshot()

    print("")
    print(
        "===================================="
    )
    print(
        "EXTRACCION FINALIZADA OK"
    )
    print(
        "===================================="
    )

    print(
        "LV:",
        metadata[
            "lv_unique_records"
        ],
    )

    print(
        "ALL:",
        metadata[
            "all_unique_records"
        ],
    )

    print(
        "JSON:",
        metadata[
            "json_records"
        ],
    )

    print(
        "CSV:",
        metadata[
            "csv_records"
        ],
    )

    print(
        "Coverage complete:",
        metadata[
            "coverage_complete"
        ],
    )

    print(
        "Validation status:",
        metadata[
            "validation_status"
        ],
    )

    print("")
    print(
        "Snapshot nuevo publicado."
    )


if __name__ == "__main__":
    main()
