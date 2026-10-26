# -*- coding: utf-8 -*-
"""
Robot de los feriados (26/10/2026).

La app calcula los feriados nacionales con la Ley 27.399 (inamovibles,
trasladables con su regla de corrimiento y los de Pascua). Eso no alcanza:
cada año un decreto fija el calendario de verdad, y a veces agrega días que
ninguna regla calcula. El caso que lo pidió: el 09/11/2026, feriado nacional
por la visita del Papa León XIV, que la app tomaba como un lunes común (las
extras al 50 % en vez del 100 %).

Fuente: el calendario oficial de Jefatura de Gabinete
(argentina.gob.ar/jefatura/feriados-nacionales-AAAA), que publica un JSON por
año en /sites/default/files/holidays-AAAA-es.json. El archivo de un año
aparece recién cuando ese año está decretado (el de 2027 da 404 al
26/10/2026): su sola presencia dice "este año ya está definido". Mientras no
aparece, la app sigue con su cálculo por la ley.

Control cruzado: ArgentinaDatos (api.argentinadatos.com/v1/feriados/AAAA)
tiene que decir lo mismo en los feriados nacionales y en los puentes. Por eso
publica solo (commit directo, como el tope del SIPA): dos fuentes tienen que
coincidir antes de que un día cambie de precio.
  - Si una de las dos no contesta, el script termina con error y NO toca nada
    (mañana vuelve a probar).
  - Si contestan distinto, ESE año no se toca (queda lo que había) y se anota
    en desacuerdos-feriados.txt: el workflow abre un issue para que lo mire
    una persona. Pasó de verdad con 2025: el calendario oficial tiene el
    10/10/2025 como feriado (el Día de la Diversidad corrido al viernes) y
    ArgentinaDatos como puente.

Lo que escribe en parametros.json:
  feriadosNacionales  {"AAAA": [{fecha, nombre}]}: los feriados nacionales del
                      año (inamovibles y trasladables) como quedaron por
                      decreto. La app los usa EN LUGAR de su cálculo para ese
                      año: así entran los agregados y los trasladables que un
                      decreto corre distinto de la regla.
  diasNoLaborables    [{fecha, nombre}]: los puentes turísticos. NO son
                      feriados (art. 167 LCT): la app solo los nombra. Solo
                      los que quedan por delante (pedido del dueño, 01/10/2026);
                      los ya cargados se conservan. Reemplaza a la tarea
                      programada `puentes-2027-pizarra`, que los cargaba a mano.
  feriadosRegionales  [{fecha, nombre, donde}]: feriados que rigen solo en
                      algunas provincias ("especial" en el calendario). La app
                      no sabe dónde trabaja cada uno: se los ofrece para que
                      los marque como propios con un toque.
No escribe los días no laborables religiosos ("no_laborable"): rigen solo
para quien profesa ese culto y la app no puede saberlo.

Años: el de hoy y el siguiente. Los de otros años que ya estén en el archivo
se conservan (el Historial muestra períodos pasados).

Guardarraíles (cualquiera que falle = error, sin cambios):
  - cada fecha es del año que dice el archivo;
  - están los nueve inamovibles fijos de la ley (01/01, 24/03, 02/04, 01/05,
    25/05, 20/06, 09/07, 08/12, 25/12);
  - entre 14 y 22 feriados nacionales por año;
  - tipos conocidos: un tipo nuevo puede ser un feriado que no sabemos tratar.
"""
import html
import json
import re
import sys
from datetime import date

import requests

OFICIAL = "https://www.argentina.gob.ar/sites/default/files/holidays-{anio}-es.json"
DESACUERDOS = "desacuerdos-feriados.txt"
ARGENTINA_DATOS = "https://api.argentinadatos.com/v1/feriados/{anio}"

INAMOVIBLES_FIJOS = ["01-01", "03-24", "04-02", "05-01", "05-25", "06-20", "07-09", "12-08", "12-25"]
TIPOS_NACIONALES = {"inamovible", "trasladable"}
TIPOS_CONOCIDOS = TIPOS_NACIONALES | {"turistico", "no_laborable", "especial"}
NOMBRE_PUENTE = "Puente turístico"


def texto_plano(s):
    """Saca el HTML que trae el nombre de los especiales y normaliza espacios."""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s or ""))).strip()


def bajar_oficial(anio):
    """Los días del año según Jefatura de Gabinete, o None si el año no está decretado."""
    r = requests.get(OFICIAL.format(anio=anio), timeout=60)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    datos = r.json()
    dias = []
    for elemento in datos["mainEntity"]["itemListElement"]:
        item = elemento["item"]
        fecha = item["startDate"][:10]
        if not fecha.startswith(f"{anio}-"):
            continue  # el archivo de 2026 trae también el 01/01/2027
        tipo = item["additionalProperty"]["value"]
        if tipo not in TIPOS_CONOCIDOS:
            raise RuntimeError(f"{anio}: tipo de día desconocido «{tipo}» el {fecha}")
        dias.append({"fecha": fecha, "tipo": tipo, "nombre": texto_plano(item["name"])})
    return dias


def bajar_argentina_datos(anio):
    r = requests.get(ARGENTINA_DATOS.format(anio=anio), timeout=60)
    r.raise_for_status()
    return r.json()


def separar(anio, oficial):
    """Los nacionales, los puentes y los regionales de un año, ya validados."""
    nacionales, puentes, regionales = {}, set(), []
    for d in oficial:
        if d["tipo"] in TIPOS_NACIONALES:
            # Un mismo día puede venir dos veces (02/04/2026: Malvinas y Jueves
            # Santo); queda el primero, que es el feriado nacional.
            nacionales.setdefault(d["fecha"], d["nombre"])
        elif d["tipo"] == "turistico":
            puentes.add(d["fecha"])
        elif d["tipo"] == "especial":
            m = re.match(r"^(.*?)\s*\(\s*feriado en (.+?)\s*\)\s*$", d["nombre"], re.I)
            if not m:
                raise RuntimeError(f"{anio}: no entiendo dónde rige el especial del {d['fecha']}: {d['nombre']}")
            regionales.append({"fecha": d["fecha"], "nombre": m.group(1), "donde": m.group(2)})

    faltan = [f for f in INAMOVIBLES_FIJOS if f"{anio}-{f}" not in nacionales]
    if faltan:
        raise RuntimeError(f"{anio}: al calendario oficial le faltan inamovibles de la ley: {faltan}")
    if not 14 <= len(nacionales) <= 22:
        raise RuntimeError(f"{anio}: {len(nacionales)} feriados nacionales, fuera de lo razonable")
    # Los puentes caen en día hábil y nunca sobre un feriado.
    for p in puentes:
        if p in nacionales or date.fromisoformat(p).weekday() >= 5:
            raise RuntimeError(f"{anio}: el puente del {p} cae en feriado o fin de semana")
    return nacionales, puentes, regionales


def desacuerdo(anio, nacionales, puentes, otra):
    """None si ArgentinaDatos tiene los mismos feriados nacionales y puentes; si no, qué difiere."""
    sus_nacionales = {d["fecha"] for d in otra if d.get("tipo") in TIPOS_NACIONALES}
    sus_puentes = {d["fecha"] for d in otra if d.get("tipo") == "puente"}
    if sus_nacionales == set(nacionales) and sus_puentes == puentes:
        return None
    return (
        f"{anio}: feriados solo en el oficial {sorted(set(nacionales) - sus_nacionales)}, "
        f"solo en ArgentinaDatos {sorted(sus_nacionales - set(nacionales))}; "
        f"puentes solo en el oficial {sorted(puentes - sus_puentes)}, "
        f"solo en ArgentinaDatos {sorted(sus_puentes - puentes)}"
    )


def main():
    hoy = date.today()
    with open("parametros.json", encoding="utf-8") as f:
        actual = json.load(f)

    feriados = dict(actual.get("feriadosNacionales") or {})
    puentes_por_anio = {}
    for d in actual.get("diasNoLaborables") or []:
        puentes_por_anio.setdefault(d["fecha"][:4], []).append(d)
    regionales_por_anio = {}
    for d in actual.get("feriadosRegionales") or []:
        regionales_por_anio.setdefault(d["fecha"][:4], []).append(d)

    decretados, desacuerdos = [], []
    for anio in (hoy.year, hoy.year + 1):
        oficial = bajar_oficial(anio)
        if oficial is None:
            print(f"{anio}: todavía no está decretado; la app sigue con la ley.")
            continue
        nacionales, puentes, regionales = separar(anio, oficial)
        distinto = desacuerdo(anio, nacionales, puentes, bajar_argentina_datos(anio))
        if distinto:
            print(f"Las fuentes no coinciden, {anio} queda como estaba: {distinto}")
            desacuerdos.append(distinto)
            continue
        feriados[str(anio)] = [{"fecha": f, "nombre": n} for f, n in sorted(nacionales.items())]
        # Puentes: solo los que quedan por delante (pedido del dueño del
        # 01/10/2026: «los que queden»). Los ya cargados se conservan, salvo
        # uno por venir que el calendario oficial ya no trae.
        hoy_iso = hoy.isoformat()
        conservados = {
            d["fecha"]: d for d in puentes_por_anio.get(str(anio), []) if d["fecha"] < hoy_iso or d["fecha"] in puentes
        }
        for p in puentes:
            if p >= hoy_iso:
                conservados.setdefault(p, {"fecha": p, "nombre": NOMBRE_PUENTE})
        puentes_por_anio[str(anio)] = [conservados[f] for f in sorted(conservados)]
        regionales_por_anio[str(anio)] = sorted(regionales, key=lambda d: (d["fecha"], d["donde"]))
        decretados.append(anio)

    if desacuerdos:
        with open(DESACUERDOS, "w", encoding="utf-8") as f:
            f.write("\n".join(desacuerdos) + "\n")

    nuevo = json.loads(json.dumps(actual))
    nuevo["feriadosNacionales"] = dict(sorted(feriados.items()))
    nuevo["diasNoLaborables"] = [d for a in sorted(puentes_por_anio) for d in puentes_por_anio[a]]
    nuevo["feriadosRegionales"] = [d for a in sorted(regionales_por_anio) for d in regionales_por_anio[a]]
    fuentes = nuevo.setdefault("fuentes", {})
    fuentes["feriados"] = (
        "Calendario oficial de Jefatura de Gabinete (argentina.gob.ar/jefatura/feriados-nacionales-AAAA, "
        "holidays-AAAA-es.json), controlado contra ArgentinaDatos. Robot diario (scripts/feriados.py). "
        f"Años decretados al {hoy.isoformat()}: {', '.join(map(str, decretados)) or 'ninguno'}."
    )
    fuentes["diasNoLaborables"] = (
        "Art. 7 Ley 27.399: hasta 3 días no laborables con fines turísticos por año. NO son feriados "
        "(art. 167 LCT: si se trabaja se paga como día común). Los publica el robot de feriados con el "
        "calendario oficial, solo los que quedan por delante."
    )

    sin_meta = lambda d: {k: v for k, v in d.items() if k not in ("version", "actualizado", "fuentes")}  # noqa: E731
    if sin_meta(nuevo) == sin_meta(actual):
        print("Sin cambios: los feriados ya están publicados.")
        return

    nuevo["version"] = actual["version"] + 1
    nuevo["actualizado"] = hoy.isoformat()
    with open("parametros.json", "w", encoding="utf-8") as f:
        json.dump(nuevo, f, ensure_ascii=False, indent=2)
        f.write("\n")
    for anio in decretados:
        print(f"{anio}: {len(feriados[str(anio)])} feriados nacionales, "
              f"{len(puentes_por_anio[str(anio)])} puentes, {len(regionales_por_anio[str(anio)])} regionales")
    print(f"parametros.json -> version {nuevo['version']}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"ERROR, no se tocó nada: {e}", file=sys.stderr)
        sys.exit(1)
