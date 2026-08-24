import csv
import json
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

OUTPUT_JSON = Path("latest.json")
OUTPUT_CSV = Path("latest.csv")
OUTPUT_METADATA = Path("metadata.json")

START_DATE = datetime(2024, 1, 1)

WINDOW_DAYS = 10
TIMEOUT = 60


# ============================================================
# PARAMETROS
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

        tag = normalize_tag(
            element.tag
        ).lower()

        if (
            tag in possible_names
            and list(element)
        ):
            candidates.append(element)

    if candidates:
        return candidates

    children = [
        child
        for child in list(root)
        if list(child)
    ]

    return children


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
# IDENTIFICADOR
# ============================================================

def make_record_key(record):
    flattened = {
        str(k).lower(): str(v).strip()
        for k, v in flatten(record).items()
        if v not in (None, "")
    }

    # Priorizamos identificadores oficiales
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

            if (
                key.endswith(candidate)
                and value
            ):
                return (
                    f"{candidate}:{value}"
                )

    # Fallback compuesto
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

            if (
                keyword in key
                and value
            ):

                fallback_values.append(
                    f"{keyword}:{value}"
                )

                break

    if fallback_values:

        return "|".join(
            fallback_values
        )

    return json.dumps(
        record,
        sort_keys=True,
        ensure_ascii=False
    )


# ============================================================
# VENTANAS
# ============================================================

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
# DESCARGA DE UNA VENTANA
# ============================================================

def download_window(
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

    label = (
        "LLAMADOS VIGENTES"
        if publication_type == "lv"
        else "TODOS LOS LLAMADOS"
    )

    print(
        f"[{label}] "
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

        preview = (
            response.text[:500]
        )

        raise RuntimeError(
            "ARCE no devolvio XML valido.\n"
            f"{preview}"
        ) from exc

    nodes = find_record_nodes(root)

    records = []

    for node in nodes:

        record = (
            element_to_dict(node)
        )

        if isinstance(record, dict):
            records.append(record)

    return records


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
    failed_windows = 0

    for start, end in generate_windows(
        start_date,
        end_date,
    ):

        try:

            records = download_window(
                session,
                start,
                end,
                publication_type,
            )

            all_records.extend(
                records
            )

            windows_processed += 1

        except Exception as exc:

            failed_windows += 1

            print(
                "ERROR "
                f"{publication_type} "
                f"{start:%d/%m/%Y} "
                f"- {end:%d/%m/%Y}: "
                f"{exc}",
                file=sys.stderr,
            )

    unique = {}

    for record in all_records:

        key = make_record_key(
            record
        )

        unique[key] = record

    return {
        "raw_records": all_records,
        "unique": unique,
        "windows_processed": (
            windows_processed
        ),
        "failed_windows": (
            failed_windows
        ),
    }


# ============================================================
# VALIDACION CRUZADA
# ============================================================

def cross_validate(
    lv_unique,
    all_unique,
):
    """
    Todo llamado vigente obtenido con tipo_publicacion=lv
    deberia existir tambien dentro de tipo_publicacion=l.

    Esta validacion detecta inconsistencias de universo,
    IDs o extraccion.
    """

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

    lv_is_subset_of_all = (
        len(missing_in_all) == 0
    )

    return {
        "lv_keys": lv_keys,
        "all_keys": all_keys,
        "common": common,
        "missing_in_all": (
            missing_in_all
        ),
        "lv_is_subset_of_all": (
            lv_is_subset_of_all
        ),
    }


# ============================================================
# JSON
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
# CSV
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
# METADATA
# ============================================================

def write_metadata(
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
        count_csv_records()
    )

    lv_xml_complete = (
        lv_result[
            "failed_windows"
        ] == 0
        and lv_unique_records > 0
    )

    all_xml_complete = (
        all_result[
            "failed_windows"
        ] == 0
        and all_unique_records > 0
    )

    file_integrity = (
        json_records
        == csv_records
    )

    cross_validation_match = (
        cross_result[
            "lv_is_subset_of_all"
        ]
    )

    xml_double_validation = (
        lv_xml_complete
        and all_xml_complete
        and file_integrity
        and cross_validation_match
    )

    # Importante:
    #
    # Este campo indica cobertura completa
    # respecto de la doble extraccion XML.
    #
    # No equivale aun a reconciliacion contra
    # el contador HTML del portal.
    coverage_complete = (
        xml_double_validation
    )

    if coverage_complete:

        validation_status = "OK"

        note = (
            "Cobertura XML validada por doble "
            "extraccion oficial: todas las ventanas "
            "de llamados vigentes y todos los "
            "llamados fueron procesadas sin error; "
            "todos los llamados vigentes existen "
            "tambien en el universo de todos los "
            "llamados; JSON y CSV coinciden. "
            "No incluye reconciliacion contra "
            "el contador HTML del portal."
        )

    else:

        validation_status = "ERROR"

        note = (
            "La doble validacion XML detecto "
            "errores o inconsistencias. "
            "No se certifica cobertura."
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
        "source_endpoint": (
            BASE_URL
        ),
        "start_date": (
            START_DATE.strftime(
                "%Y-%m-%d"
            )
        ),

        # LLAMADOS VIGENTES
        "lv_windows_processed": (
            lv_result[
                "windows_processed"
            ]
        ),
        "lv_failed_windows": (
            lv_result[
                "failed_windows"
            ]
        ),
        "lv_raw_records": len(
            lv_result[
                "raw_records"
            ]
        ),
        "lv_unique_records": (
            lv_unique_records
        ),

        # TODOS LOS LLAMADOS
        "all_windows_processed": (
            all_result[
                "windows_processed"
            ]
        ),
        "all_failed_windows": (
            all_result[
                "failed_windows"
            ]
        ),
        "all_raw_records": len(
            all_result[
                "raw_records"
            ]
        ),
        "all_unique_records": (
            all_unique_records
        ),

        # CRUCE
        "lv_records_found_in_all": len(
            cross_result[
                "common"
            ]
        ),
        "lv_records_missing_in_all": len(
            cross_result[
                "missing_in_all"
            ]
        ),
        "lv_is_subset_of_all": (
            cross_validation_match
        ),

        # ARCHIVOS
        "json_records": (
            json_records
        ),
        "csv_records": (
            csv_records
        ),
        "file_integrity": (
            file_integrity
        ),

        # CONTROLES
        "lv_xml_complete": (
            lv_xml_complete
        ),
        "all_xml_complete": (
            all_xml_complete
        ),
        "cross_validation_match": (
            cross_validation_match
        ),
        "xml_double_validation": (
            xml_double_validation
        ),

        # FRONTEND
        "portal_total": None,
        "portal_reconciliation": False,

        # RESULTADO
        "coverage_complete": (
            coverage_complete
        ),
        "coverage_basis": (
            "DOUBLE_OFFICIAL_XML_EXTRACTION"
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
    today = (
        datetime.now().date()
    )

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
                "application/xml,"
                "text/xml,"
                "*/*;q=0.8"
            ),
            "Accept-Language": (
                "es-UY,es;q=0.9,en;q=0.8"
            ),
        }
    )

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

    # --------------------------------------------------------
    # VALIDACION
    # --------------------------------------------------------

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

    print(
        "LV subset de ALL:",
        cross_result[
            "lv_is_subset_of_all"
        ],
    )

    # --------------------------------------------------------
    # ARCHIVOS FINALES
    # --------------------------------------------------------

    records = list(
        lv_result[
            "unique"
        ].values()
    )

    write_json(records)
    write_csv(records)

    write_metadata(
        lv_result=lv_result,
        all_result=all_result,
        cross_result=cross_result,
    )

    # --------------------------------------------------------
    # VALIDACIONES DE ERROR
    # --------------------------------------------------------

    if (
        lv_result[
            "failed_windows"
        ] > 0
    ):
        print(
            "ERROR: hubo ventanas fallidas "
            "en llamados vigentes."
        )
        sys.exit(1)

    if (
        all_result[
            "failed_windows"
        ] > 0
    ):
        print(
            "ERROR: hubo ventanas fallidas "
            "en todos los llamados."
        )
        sys.exit(1)

    if not records:
        print(
            "ERROR: no se obtuvieron "
            "llamados vigentes."
        )
        sys.exit(1)

    if not cross_result[
        "lv_is_subset_of_all"
    ]:

        print(
            "ERROR: existen llamados vigentes "
            "que no aparecen en todos los llamados."
        )

        print(
            "Primeros faltantes:"
        )

        for key in (
            cross_result[
                "missing_in_all"
            ][:20]
        ):
            print("-", key)

        sys.exit(1)

    print("")
    print(
        "===================================="
    )
    print(
        "EXTRACCION FINALIZADA"
    )
    print(
        "===================================="
    )

    print(
        "latest.json:",
        len(records),
        "registros"
    )

    print(
        "latest.csv:",
        count_csv_records(),
        "registros"
    )

    print(
        "Doble validacion XML: OK"
    )

    print("")
    print(
        "IMPORTANTE:"
    )

    print(
        "coverage_complete=true significa "
        "cobertura validada mediante las dos "
        "consultas XML oficiales de ARCE."
    )

    print(
        "La reconciliacion contra el contador "
        "HTML del portal permanece separada."
    )


if __name__ == "__main__":
    main()
