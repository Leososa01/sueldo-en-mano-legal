# -*- coding: utf-8 -*-
"""
Robot de parámetros fiscales de Sueldo en Mano.

Corre una vez por mes (GitHub Action). Baja los PDFs oficiales de ARCA
(escala del art. 94 y deducciones del art. 30), extrae los valores, los
VALIDA matemáticamente y, si cambiaron respecto de parametros.json, escribe
el archivo nuevo. El workflow abre un Pull Request para revisión humana:
nunca se publica un número impositivo sin que alguien lo mire.

Si algo no se puede leer o no cierra, el script termina con error y NO toca
nada (la app sigue con los últimos valores buenos).
"""
import io
import json
import re
import sys
from datetime import date

import pdfplumber
import requests

BASE = "https://www.afip.gob.ar/gananciasYBienes/ganancias/personas-humanas-sucesiones-indivisas"

ALICUOTAS = [0.05, 0.09, 0.12, 0.15, 0.19, 0.23, 0.27, 0.31, 0.35]


def bajar(url):
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    if not r.content.startswith(b"%PDF"):
        raise RuntimeError(f"No es un PDF: {url}")
    return io.BytesIO(r.content)


def num(texto):
    """'2.168.491,89' -> 2168491.89"""
    return float(texto.replace(".", "").replace(",", "."))


def urls_semestre(hoy):
    """URLs de los PDF del semestre vigente (con proyección en el 1er semestre)."""
    anio = hoy.year
    if hoy.month >= 7:
        return (
            f"{BASE}/declaracion-jurada/documentos/Tabla-Art-94-LIG-per-jul-a-dic-{anio}.pdf",
            f"{BASE}/deducciones/documentos/Deducciones-personales-art-30-jul-dic-{anio}.pdf",
            False,  # la tabla ya es anual
        )
    return (
        f"{BASE}/declaracion-jurada/documentos/Tabla-Art-94-LIG-per-ene-a-jun-{anio}.pdf",
        f"{BASE}/deducciones/documentos/Deducciones-personales-art-30-ene-a-jun-{anio}.pdf",
        True,  # solo hay medio año publicado: se proyecta ×2 hasta que salga julio
    )


def extraer_escala(pdf_bytes, proyectar):
    """Devuelve la escala anual: la tabla con el tramo inicial más grande del PDF."""
    filas_por_pagina = []
    with pdfplumber.open(pdf_bytes) as pdf:
        for page in pdf.pages:
            texto = page.extract_text() or ""
            filas = []
            for linea in texto.splitlines():
                # fila de escala: desde hasta fijo alicuota excedente
                m = re.findall(r"[\d.]+,\d{2}|\b\d{1,2}\b", linea)
                if len(m) == 5 and m[3].isdigit():
                    try:
                        filas.append((num(m[0]), num(m[1]), num(m[2]), int(m[3]) / 100.0))
                    except ValueError:
                        pass
                # última fila ("en adelante"): desde fijo alicuota excedente
                elif len(m) == 4 and m[2].isdigit():
                    try:
                        filas.append((num(m[0]), None, num(m[1]), int(m[2]) / 100.0))
                    except ValueError:
                        pass
            if len(filas) == 9:
                filas_por_pagina.append(filas)

    if not filas_por_pagina:
        raise RuntimeError("No encontré ninguna tabla de 9 tramos en el PDF de escala")

    # La tabla "más acumulada" (mayor primer tope) es la anual (o junio en el 1er sem.)
    tabla = max(filas_por_pagina, key=lambda t: t[0][1] or 0)
    factor = 2 if proyectar else 1
    escala = [
        {"desde": round(f[0] * factor, 2), "fijo": round(f[2] * factor, 2), "alicuota": f[3]}
        for f in tabla
    ]

    # Validaciones duras
    if [t["alicuota"] for t in escala] != ALICUOTAS:
        raise RuntimeError(f"Alícuotas inesperadas: {[t['alicuota'] for t in escala]}")
    for i in range(1, 9):
        esperado = escala[i - 1]["fijo"] + (escala[i]["desde"] - escala[i - 1]["desde"]) * escala[i - 1]["alicuota"]
        if abs(escala[i]["fijo"] - esperado) > 2:
            raise RuntimeError(f"El monto fijo del tramo {i} no cierra: {escala[i]['fijo']} vs {esperado:.2f}")
    return escala


MONEDA = re.compile(r"\b\d{1,3}(?:\.\d{3}){1,4},\d{2}\b")


def extraer_deducciones(pdf_bytes, proyectar):
    """
    Lee la PRIMERA página del PDF de deducciones: es el resumen "importe de la
    deducción a diciembre" (o a junio en el 1er semestre). El número puede
    quedar en la misma línea que el concepto, en la anterior o la siguiente
    (el PDF intercala columnas), así que se busca por ventana de líneas.
    """
    with pdfplumber.open(pdf_bytes) as pdf:
        lineas = (pdf.pages[0].extract_text() or "").splitlines()

    def buscar(patron):
        rx = re.compile(patron, re.I)
        for i, linea in enumerate(lineas):
            if rx.search(linea):
                # misma línea primero; después la siguiente y la anterior
                for candidata in (linea, *lineas[i + 1 : i + 2], *lineas[i - 1 : i]):
                    m = MONEDA.search(candidata)
                    if m:
                        return num(m.group(0))
        raise RuntimeError(f"No encontré el concepto: {patron}")

    factor = 2 if proyectar else 1
    gni = buscar(r"no imponibles?\s*\[Art") * factor
    conyuge = buscar(r"C.nyuge:") * factor
    hijo = buscar(r"^2\.\s*Hijo:") * factor
    hijo_inc = buscar(r"Hijo incapacitado") * factor
    ded_esp = buscar(r"Apartado 2\]") * factor

    relacion = ded_esp / gni
    if not (4.5 <= relacion <= 5.1):
        raise RuntimeError(f"Deducción especial / GNI = {relacion:.2f} (esperaba ~4,8)")
    return {
        "gniAnual": round(gni, 2),
        "deduccionEspecialAnual": round(ded_esp, 2),
        "conyugeAnual": round(conyuge, 2),
        "hijoAnual": round(hijo, 2),
        "hijoIncapacitadoAnual": round(hijo_inc, 2),
        "topeAlquilerAnual": round(gni, 2),
        "topeDomesticoAnual": round(gni, 2),
    }


def main():
    hoy = date.today()
    url_escala, url_deducciones, proyectar = urls_semestre(hoy)
    # Tope SIPA opcional por línea de comandos (workflow_dispatch manual).
    # 1er argumento: el tope. 2do (opcional): desde qué mes rige, "AAAA-MM".
    # Por defecto el mes de hoy — pasalo a mano si cargás el tope tarde (el de
    # agosto publicado el 2 de septiembre tiene que quedar como agosto).
    tope_manual = float(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1] else None
    mes_tope = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] else hoy.strftime("%Y-%m")
    if not re.fullmatch(r"\d{4}-\d{2}", mes_tope):
        raise RuntimeError(f"El mes del tope tiene que ser AAAA-MM, vino: {mes_tope}")

    with open("parametros.json", encoding="utf-8") as f:
        actual = json.load(f)

    escala = extraer_escala(bajar(url_escala), proyectar)
    deducciones = extraer_deducciones(bajar(url_deducciones), proyectar)

    nuevo = dict(actual)
    # OJO: se PARTE de las ganancias actuales y recién encima se pisan las que el
    # robot sabe leer. Si se reemplazara el objeto entero, cada corrida borraría
    # los campos que este script NO extrae de los PDF de ARCA — hoy
    # `topeSeguroVidaAnual` (art. 85 inc. b, que vive en otra tabla y se carga a
    # mano). Sería un bug silencioso: la corrida del lunes borra el campo y la
    # app cae a su valor de fábrica sin avisar nada.
    nuevo["ganancias"] = dict(actual.get("ganancias", {}), **deducciones, escalaAnual=escala)
    if tope_manual:
        # El tope del SIPA lleva su PROPIA fecha: ANSES lo mueve todos los meses,
        # mientras que el `vigenciaDesde` de abajo es el de las tablas de ARCA,
        # que cambian por semestre. La app archiva el tope en el mes que dice acá
        # para armar el historial que necesita Ganancias; si compartieran fecha,
        # los seis topes del semestre se pisarían entre ellos.
        nuevo["aportes"] = {
            "baseImponibleTope": tope_manual,
            "vigenciaDesde": f"{mes_tope}-01",
        }
    nuevo["fuentes"] = dict(actual.get("fuentes", {}), escala=url_escala, deducciones=url_deducciones)

    sin_meta_actual = {k: v for k, v in actual.items() if k not in ("version", "actualizado")}
    sin_meta_nuevo = {k: v for k, v in nuevo.items() if k not in ("version", "actualizado")}
    if sin_meta_actual == sin_meta_nuevo:
        print("Sin cambios: la pizarra ya está al día.")
        return

    nuevo["version"] = actual["version"] + 1
    nuevo["actualizado"] = hoy.isoformat()
    nuevo["vigenciaDesde"] = f"{hoy.year}-07-01" if hoy.month >= 7 else f"{hoy.year}-01-01"

    with open("parametros.json", "w", encoding="utf-8") as f:
        json.dump(nuevo, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"parametros.json actualizado a la versión {nuevo['version']}")


if __name__ == "__main__":
    main()
