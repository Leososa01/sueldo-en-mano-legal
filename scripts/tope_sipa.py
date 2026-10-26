# -*- coding: utf-8 -*-
"""
Robot del tope del SIPA (base imponible máxima de aportes).

ANSES lo mueve TODOS los meses por la movilidad del DNU 274/2024: el tope del
mes M es el del mes M-1 multiplicado por (1 + IPC del mes M-2), con la
variación del IPC redondeada a dos decimales de porcentaje, como la publica
la propia resolución ("movilidad 2,11%"). Verificado contra cuatro
resoluciones seguidas (186, 232, 257 y 284 de 2026): la cuenta reproduce el
valor oficial AL CENTAVO, así que no hace falta leer el Boletín Oficial.

El IPC sale de la API pública de datos.gob.ar (serie del INDEC, nivel general
nacional, base dic-2016). Si la API no responde o el número no cierra, el
script termina con error y NO toca nada: la app sigue con el último tope
bueno.

Lo que escribe en parametros.json:
  aportes.baseImponibleTope  el tope del mes en curso (o del último calculable)
  aportes.vigenciaDesde      "AAAA-MM-01" de ese tope
  aportes.historico          {"AAAA-MM": tope} con todos los meses conocidos,
                             para que la app pueda rellenar los meses que no
                             vio (abre la app cada tanto, no todos los meses)
  fuentes.topeSipa           cómo se calculó

Guardarraíles (cualquiera que falle = error, sin cambios):
  - la movilidad mensual tiene que estar entre 0% y 15%;
  - el tope solo sube;
  - no se publica un tope de un mes FUTURO: el de octubre sale el 1 de
    octubre, aunque el IPC de agosto esté desde mediados de septiembre;
  - el IPC usado tiene que ser exactamente el del mes M-2.

EL TOPE DEL SEGURO DE VIDA (26/10/2026). Ya que este robot baja el IPC,
también lleva el tope anual de las primas de seguro de vida de Ganancias
(art. 85 inc. b): se actualiza una vez por año por la variación del IPC de
octubre contra octubre del año anterior. Verificado al centavo: 573.817,13
(2025) × IPC oct-2025 / oct-2024 = 753.472,14 (2026). Se publica el 1 de
enero de cada año, nunca antes (antes de esa fecha el año que rige es el
anterior), en ganancias.topeSeguroVidaAnual, con su año en
ganancias.topeSeguroVidaAnio.
"""
import json
import sys
from datetime import date

import requests

SERIE_IPC = "148.3_INIVELNAL_DICI_M_26"  # IPC nivel general nacional, base dic-2016
API = "https://apis.datos.gob.ar/series/api/series/"


def r2(x):
    return round(x + 1e-9, 2)


def mes_siguiente(ym):
    a, m = int(ym[:4]), int(ym[5:7])
    return f"{a + 1}-01" if m == 12 else f"{a}-{m + 1:02d}"


def mes_menos(ym, n):
    a, m = int(ym[:4]), int(ym[5:7])
    idx = a * 12 + (m - 1) - n
    return f"{idx // 12}-{idx % 12 + 1:02d}"


def bajar_ipc(limite=24):
    """{'AAAA-MM': índice} de los últimos `limite` meses publicados."""
    r = requests.get(
        API,
        params={"ids": SERIE_IPC, "limit": limite, "sort": "desc", "format": "json"},
        timeout=60,
    )
    r.raise_for_status()
    datos = r.json().get("data") or []
    ipc = {}
    for fecha, valor in datos:
        if valor is None:
            continue
        ipc[fecha[:7]] = float(valor)
    if len(ipc) < 3:
        raise RuntimeError(f"La API del IPC devolvió muy poco: {datos!r}")
    return ipc


def movilidad(ipc, mes):
    """Movilidad del mes `mes`: variación del IPC de M-2 contra M-3, en % a 2 decimales."""
    m2, m3 = mes_menos(mes, 2), mes_menos(mes, 3)
    if m2 not in ipc or m3 not in ipc:
        return None
    pct = r2((ipc[m2] / ipc[m3] - 1) * 100)
    if not (0 <= pct <= 15):
        raise RuntimeError(f"Movilidad fuera de rango para {mes}: {pct}% (IPC {m3}→{m2})")
    return pct


def calcular(actual, ipc, hoy):
    """Devuelve (historico_nuevo, movilidades) extendiendo el histórico hasta el mes de hoy."""
    aportes = actual["aportes"]
    historico = dict(aportes.get("historico") or {})
    # Semilla: el tope actual, si el histórico no lo tiene.
    mes_actual = (aportes.get("vigenciaDesde") or actual["vigenciaDesde"])[:7]
    historico.setdefault(mes_actual, aportes["baseImponibleTope"])

    tope_mes_hoy = hoy.strftime("%Y-%m")
    movs = {}
    ultimo = max(historico)
    while ultimo < tope_mes_hoy:
        siguiente = mes_siguiente(ultimo)
        pct = movilidad(ipc, siguiente)
        if pct is None:
            break  # el IPC de M-2 todavía no salió: se espera
        tope = r2(historico[ultimo] * (1 + pct / 100))
        if tope <= historico[ultimo]:
            raise RuntimeError(f"El tope de {siguiente} no sube: {tope} vs {historico[ultimo]}")
        historico[siguiente] = tope
        movs[siguiente] = pct
        ultimo = siguiente
    return dict(sorted(historico.items())), movs


# El valor que se cargó a mano en la pizarra v3 es el del período fiscal 2026.
ANIO_SEGURO_VIDA_CARGADO = 2026


def seguro_de_vida(ganancias, ipc, hoy):
    """
    (año, tope) del seguro de vida del año de hoy, llevado por IPC de octubre
    a octubre desde el último publicado; None si ya está al día.
    """
    anio = int(ganancias.get("topeSeguroVidaAnio") or ANIO_SEGURO_VIDA_CARGADO)
    tope = ganancias.get("topeSeguroVidaAnual")
    if tope is None or anio >= hoy.year:
        return None
    while anio < hoy.year:
        anio += 1
        oct_antes, oct_antes2 = f"{anio - 1}-10", f"{anio - 2}-10"
        if oct_antes not in ipc or oct_antes2 not in ipc:
            raise RuntimeError(f"Falta el IPC de {oct_antes2} o {oct_antes} para el seguro de vida de {anio}")
        factor = ipc[oct_antes] / ipc[oct_antes2]
        if not (1.0 <= factor <= 4.0):
            raise RuntimeError(f"Factor del seguro de vida de {anio} fuera de rango: {factor:.4f}")
        tope = r2(tope * factor)
    return anio, tope


def main():
    hoy = date.today()
    with open("parametros.json", encoding="utf-8") as f:
        actual = json.load(f)

    ipc = bajar_ipc()
    historico, movs = calcular(actual, ipc, hoy)
    ultimo = max(historico)

    nuevo = json.loads(json.dumps(actual))
    nuevo["aportes"] = {
        "baseImponibleTope": historico[ultimo],
        "vigenciaDesde": f"{ultimo}-01",
        "historico": historico,
    }
    seguro = seguro_de_vida(actual.get("ganancias") or {}, ipc, hoy)
    if seguro:
        anio_seguro, tope_seguro = seguro
        nuevo["ganancias"]["topeSeguroVidaAnual"] = tope_seguro
        nuevo["ganancias"]["topeSeguroVidaAnio"] = anio_seguro
        nuevo.setdefault("fuentes", {})["topeSeguroVida"] = (
            f"Art. 85 inc. b LIG - tope anual del período fiscal {anio_seguro}, actualizado por el robot "
            f"con el IPC INDEC de octubre a octubre (datos.gob.ar, serie {SERIE_IPC})."
        )
    if movs:
        detalle = ", ".join(f"{m}: {p:.2f}%" for m, p in movs.items())
        nuevo.setdefault("fuentes", {})["topeSipa"] = (
            f"Movilidad DNU 274/2024 = IPC INDEC de M-2 (datos.gob.ar, serie {SERIE_IPC}). "
            f"Calculado por el robot: {detalle}. Reproduce al centavo las Res. ANSES 2026."
        )

    if nuevo["aportes"] == actual.get("aportes") and not seguro:
        print(f"Sin cambios: el tope de {ultimo} ya está publicado.")
        return

    nuevo["version"] = actual["version"] + 1
    nuevo["actualizado"] = hoy.isoformat()
    with open("parametros.json", "w", encoding="utf-8") as f:
        json.dump(nuevo, f, ensure_ascii=False, indent=2)
        f.write("\n")
    for m, p in movs.items():
        print(f"{m}: movilidad {p:.2f}% -> tope {historico[m]:,.2f}")
    if seguro:
        print(f"Seguro de vida {seguro[0]}: tope anual {seguro[1]:,.2f}")
    print(f"parametros.json -> version {nuevo['version']}, tope vigente {ultimo}: {historico[ultimo]:,.2f}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"ERROR, no se tocó nada: {e}", file=sys.stderr)
        sys.exit(1)
