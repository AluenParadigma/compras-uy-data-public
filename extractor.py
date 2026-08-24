import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import xml.etree.ElementTree as ET

import requests


# Permite leer campos CSV grandes provenientes del XML de ARCE
csv.field_size_limit(sys.maxsize)


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

# Mantenemos el mismo horizonte que venia funcionando.
START_DATE = datetime(2024, 1, 1)

# ARCE admite rangos de hasta 10 dias.
WINDOW_DAYS = 10

TIMEOUT = 60


# ============================================================
# PARAMETROS DE CONSULTA
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
    """
    Elimina namespace XML si existiera.
    """
    if "}" in tag:
        return tag.split("}", 1)[1]

    return tag


def xml_element_to_dict(element):
    """
    Convierte un nodo XML en diccionario.

    IMPORTANTE:
    ARCE guarda gran parte de la informacion en atributos XML.
    Por eso se incorporan primero element.attrib.

    Ejemplo conceptual:

    <compra id_compra="123"
            desc_compra="Consultoria..."
            fecha_publicacion="...">

    pasa a:

    {
        "id_compra": "123",
        "desc_compra": "Consultoria...",
        "fecha_publicacion": "...",
        ...
    }
    """

    result = {}

    # --------------------------------------------------------
    # ATRIBUTOS DEL NODO
    # --------------------------------------------------------

    for key, value in element.attrib.items():
        result[normalize_tag(key)] = value

    # --------------------------------------------------------
    # TEXTO DEL NODO
    # --------------------------------------------------------

    text = (element.text or "").strip()

    if text:
        result["_text"] = text

    # --------------------------------------------------------
    # HIJOS
    # --------------------------------------------------------

    for child in list(element):

        key = normalize_tag(child.tag)

        child_value = xml_element_to_dict(child)

        # Nodo vacio
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
    """
    La documentacion oficial de ARCE define cada registro
    de compra mediante nodos <compra>.

    No intentamos adivinar otros nodos como item,
    aclaracion o registro, porque eso genero el problema
    de la version anterior.
    """

    compras = []

    for element in root.iter():

        tag = normalize_tag(
            element.tag
        ).lower()

        if tag == "compra":
            compras.append(element)

    return compras


# ============================================================
# FLATTEN PARA CSV
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

                # Conservamos listas complejas como JSON.
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
# CALIDAD DEL REGISTRO
# ============================================================

def count_non_empty_scalar_fields(record):
    """
    Cuenta campos simples con informacion.
    Sirve para evitar volver a aceptar registros que solo
    contienen estructuras vacias.
    """

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
    """
    Un registro de compra debe contener informacion sustantiva.

    Usamos un umbral bajo porque los esquemas pueden variar,
    pero ya evita aceptar objetos practicamente vacios.
    """

    return count_non_empty_scalar_fields(record) >= 3


# ============================================================
# IDENTIFICADOR / DEDUPLICACION
# ============================================================

def make_record_key(record):
    """
    Prioriza identificadores oficiales de ARCE.

    Si cambian los nombres de campos, utiliza una combinacion
    de atributos frecuentes y finalmente el contenido completo.
    """

    flattened = {
        str(k).lower(): str(v).strip()
        for k, v in flatten(record).items()
        if v not in (None, "")
    }

    # --------------------------------------------------------
    # IDENTIFICADORES DIRECTOS
    # --------------------------------------------------------

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

    # Tambien buscamos por sufijo por si aparece anidado.
    for candidate in id_candidates:

        for key, value in flattened.items():

            if key.endswith(candidate) and value:

                return (
                    f"{candidate}:{value}"
                )

    # --------------------------------------------------------
    # CLAVE COMPUESTA
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

        for key, value in flattened.items():

            if (
                keyword in key
                and value
            ):

                fallback_values.append(
                    f"{keyword}:{value}"
                )

                break

    if len(fallback_values) >= 2:

        return "|".join(
            fallback_values
        )

    # --------------------------------------------------------
    # ULTIMO RECURSO
    # --------------------------------------------------------

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
            response.text[:1000]
        )

        raise RuntimeError(
            "ARCE no devolvio XML valido.\n"
            f"{preview}"
        ) from exc

    compra_nodes = find_compra_nodes(root)

    records = []

    for node in compra_nodes:

        record = xml_element_to_dict(
            node
        )

        if not record:
            continue

        records.append(record)

    print(
        "  compras encontradas:",
        len(records),
    )

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

    windows_with_records = 0

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

            if records:
                windows_with_records += 1

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

    # --------------------------------------------------------
    # DEDUPLICACION
    # --------------------------------------------------------

    unique = {}

    for record in all_records:

        key = make_record_key(
            record
        )

        unique[key] = record

    # --------------------------------------------------------
    # CALIDAD
    # --------------------------------------------------------

    records_with_real_content = 0

    for record in unique.values():

        if record_has_real_content(
            record
        ):
            records_with_real_content += 1

    return {
        "raw_records": all_records,
        "unique": unique,
        "windows_processed": (
            windows_processed
        ),
        "windows_with_records": (
            windows_with_records
        ),
        "failed_windows": (
            failed_windows
        ),
        "records_with_real_content": (
            records_with_real_content
        ),
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
        "missing_in_all": (
            missing_in_all
        ),
        "lv_is_subset_of_all": (
            len(missing_in_all) == 0
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

            writer.writerow(
                record
            )


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

    # --------------------------------------------------------
    # CONTROLES
    # --------------------------------------------------------

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
        == lv_unique_records
    )

    cross_validation_match = (
        cross_result[
            "lv_is_subset_of_all"
        ]
    )

    # NUEVO:
    # Todos los registros vigentes tienen que contener
    # informacion sustantiva.
    content_quality_complete = (
        lv_unique_records > 0
        and lv_content_records
        == lv_unique_records
    )

    xml_double_validation = (
        lv_xml_complete
        and all_xml_complete
        and file_integrity
        and cross_validation_match
    )

    coverage_complete = (
        xml_double_validation
        and content_quality_complete
    )

    # --------------------------------------------------------
    # ESTADO
    # --------------------------------------------------------

    if coverage_complete:

        validation_status = "OK"

        note = (
            "Cobertura XML validada por doble "
            "extraccion oficial ARCE. Todas las "
            "ventanas fueron procesadas sin error, "
            "los llamados vigentes estan contenidos "
            "en el universo general, JSON y CSV "
            "coinciden y todos los registros contienen "
            "informacion sustantiva proveniente de "
            "los atributos XML de compra."
        )

    elif xml_double_validation:

        validation_status = (
            "CONTENT_VALIDATION_ERROR"
        )

        note = (
            "La cobertura XML y la integridad de "
            "archivos son correctas, pero existen "
            "registros sin informacion sustantiva. "
            "No se certifica cobertura operativa."
        )

    else:

        validation_status = "ERROR"

        note = (
            "La extraccion presenta errores de "
            "cobertura, integridad o validacion "
            "cruzada."
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

        # -----------------------------------------------
        # LLAMADOS VIGENTES
        # -----------------------------------------------

        "lv_windows_processed": (
            lv_result[
                "windows_processed"
            ]
        ),

        "lv_windows_with_records": (
            lv_result[
                "windows_with_records"
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

        "lv_records_with_real_content": (
            lv_content_records
        ),

        # -----------------------------------------------
        # TODOS LOS LLAMADOS
        # -----------------------------------------------

        "all_windows_processed": (
            all_result[
                "windows_processed"
            ]
        ),

        "all_windows_with_records": (
            all_result[
                "windows_with_records"
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

        "all_records_with_real_content": (
            all_content_records
        ),

        # -----------------------------------------------
        # VALIDACION CRUZADA
        # -----------------------------------------------

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

        # -----------------------------------------------
        # ARCHIVOS
        # -----------------------------------------------

        "json_records": (
            json_records
        ),

        "csv_records": (
            csv_records
        ),

        "file_integrity": (
            file_integrity
        ),

        # -----------------------------------------------
        # CONTROLES
        # -----------------------------------------------

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

        "content_quality_complete": (
            content_quality_complete
        ),

        "portal_total": None,

        "portal_reconciliation": False,

        # -----------------------------------------------
        # RESULTADO
        # -----------------------------------------------

        "coverage_complete": (
            coverage_complete
        ),

        "coverage_basis": (
            "DOUBLE_OFFICIAL_XML_EXTRACTION"
            "_PLUS_CONTENT_VALIDATION"
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
# MOSTRAR EJEMPLO
# ============================================================

def print_sample_record(records):
    """
    Muestra en Actions una compra real para poder revisar
    rapidamente si ahora llegaron organismo, objeto, fechas,
    numero, etc.
    """

    if not records:

        return

    print("")
    print(
        "===================================="
    )

    print(
        "EJEMPLO DE REGISTRO EXTRAIDO"
    )

    print(
        "===================================="
    )

    sample = records[0]

    print(
        json.dumps(
            sample,
            ensure_ascii=False,
            indent=2,
        )[:5000]
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
        "LV con contenido real:",
        lv_result[
            "records_with_real_content"
        ],
    )

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

    # ========================================================
    # ARCHIVOS
    # ========================================================

    records = list(
        lv_result[
            "unique"
        ].values()
    )

    write_json(
        records
    )

    write_csv(
        records
    )

    write_metadata(
        lv_result=lv_result,
        all_result=all_result,
        cross_result=cross_result,
    )

    # Mostramos una compra para inspeccion.
    print_sample_record(
        records
    )

    # ========================================================
    # ERRORES DUROS
    # ========================================================

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
            "que no aparecen en el universo "
            "de todos los llamados."
        )

        print(
            "Primeros faltantes:"
        )

        for key in (
            cross_result[
                "missing_in_all"
            ][:20]
        ):

            print(
                "-",
                key,
            )

        sys.exit(1)

    if (
        lv_result[
            "records_with_real_content"
        ]
        != len(records)
    ):

        print(
            "ERROR: existen registros vigentes "
            "sin contenido sustantivo."
        )

        sys.exit(1)

    # ========================================================
    # OK
    # ========================================================

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
        "latest.json:",
        len(records),
        "registros",
    )

    print(
        "latest.csv:",
        count_csv_records(),
        "registros",
    )

    print(
        "Contenido XML:",
        "OK",
    )

    print(
        "Doble validacion:",
        "OK",
    )


if __name__ == "__main__":
    main()
