"""
Costos de mercadería: una sola fuente.

La fuente es la tabla contable `cont_costos` (CostoProducto): guarda la
historia (vigente_desde), el desglose de importación y las variaciones.

`config/costos.json` queda como ESPEJO del costo vigente hoy, regenerado
desde la tabla cada vez que un costo cambia. Existe porque ~36 lugares del
sistema todavía lo leen (Mis publicaciones, Calculadora, Promociones,
Alertas…); con el espejo siguen funcionando sin tocarlos y nunca muestran
un costo distinto al de Contabilidad.

Regla: nadie escribe costos.json directamente. Se escribe acá.
"""
from __future__ import annotations

import os
from datetime import date, datetime
from decimal import Decimal

from core.db_storage import db_load, db_save

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ESPEJO = os.path.join(RAIZ, 'config', 'costos.json')
MARCA_MIGRACION = os.path.join(RAIZ, 'data', '_costos_unificados.json')


def _modelo():
    from web.db import session_scope
    from web.models_contabilidad import CostoProducto
    return session_scope, CostoProducto


def vigentes(hoy: date | None = None) -> dict:
    """{item_id: {'costo', 'titulo', 'vigente_desde', 'origen'}} con el costo
    que rige hoy. Si un ítem solo tiene costos por variación, se toma el de
    la primera variación (el espejo es por publicación)."""
    session_scope, Costo = _modelo()
    hoy = hoy or date.today()
    out = {}
    with session_scope() as s:
        filas = (s.query(Costo).filter(Costo.vigente_desde <= hoy)
                 .order_by(Costo.item_id, Costo.variacion, Costo.vigente_desde.desc()).all())
        for f in filas:
            previo = out.get(f.item_id)
            # la fila sin variación manda; dentro de cada una, la más nueva
            if previo is None or (previo['_var'] != '' and f.variacion == ''):
                out[f.item_id] = {'costo': float(f.costo_unitario), 'titulo': f.titulo or '',
                                  'vigente_desde': f.vigente_desde.isoformat(),
                                  'origen': f.origen_dato, '_var': f.variacion}
    for v in out.values():
        v.pop('_var')
    return out


def sincronizar_espejo() -> int:
    """Reescribe costos.json con el costo vigente de la tabla. Conserva el
    alias que tenía cada ítem (lo usan algunas pantallas)."""
    viejo = db_load(ESPEJO) or {}
    nuevo = {}
    for iid, v in vigentes().items():
        if v['costo'] <= 0:
            continue
        nuevo[iid] = {'alias': (viejo.get(iid) or {}).get('alias', ''), 'titulo': v['titulo'],
                      'costo': v['costo'], 'updated': v['vigente_desde']}
    db_save(ESPEJO, nuevo)
    return len(nuevo)


def guardar(item_id: str, costo, *, titulo: str | None = None, alias: str | None = None,
            variacion: str = '', desde: date | None = None, origen: str = 'panel') -> None:
    """Carga o corrige el costo de una publicación.

    - Primer costo del ítem: rige desde el 1 de enero del año, para que
      Contabilidad valúe también las ventas ya hechas.
    - Ya tenía costo: el nuevo rige desde hoy y las ventas anteriores
      siguen con el costo viejo. Si se corrige el mismo día, se pisa.
    """
    _asegurar_migracion()
    session_scope, Costo = _modelo()
    item_id = str(item_id).strip().upper()
    valor = Decimal(str(costo))
    if not item_id or valor <= 0:
        raise ValueError('Costo inválido')
    with session_scope() as s:
        hay = s.query(Costo).filter_by(item_id=item_id, variacion=variacion).first() is not None
        vig = desde or (date.today() if hay else date(date.today().year, 1, 1))
        fila = s.query(Costo).filter_by(item_id=item_id, variacion=variacion, vigente_desde=vig).first()
        if fila:
            fila.costo_unitario = valor
            if titulo:
                fila.titulo = titulo[:300]
            fila.origen_dato = origen
        else:
            s.add(Costo(item_id=item_id, variacion=variacion, titulo=(titulo or '')[:300] or None,
                        costo_unitario=valor, moneda_costo='ARS', vigente_desde=vig,
                        origen_dato=origen, notas=f'Cargado desde el panel ({datetime.now():%Y-%m-%d %H:%M})'))
    sincronizar_espejo()
    if alias:
        esp = db_load(ESPEJO) or {}
        if item_id in esp and not esp[item_id].get('alias'):
            esp[item_id]['alias'] = alias
            db_save(ESPEJO, esp)


# Orígenes que se pueden borrar desde el panel. Lo cargado en Contabilidad
# (manual, excel, calculadora) es historia contable: se corrige ahí.
BORRABLES_DESDE_PANEL = ('panel', 'costos_json', 'cli')


def borrar(item_id: str) -> int:
    """Quita el costo de carga rápida de una publicación (lo que pide el
    usuario al vaciar el campo). No toca costos cargados en Contabilidad:
    borrarlos cambiaría el resultado de meses ya cerrados. Devuelve cuántas
    filas borró."""
    _asegurar_migracion()
    session_scope, Costo = _modelo()
    item_id = str(item_id).strip().upper()
    with session_scope() as s:
        n = (s.query(Costo).filter(Costo.item_id == item_id,
                                   Costo.origen_dato.in_(BORRABLES_DESDE_PANEL))
             .delete(synchronize_session=False))
    sincronizar_espejo()
    return n


def unificar_una_vez() -> dict | None:
    """Migración al arrancar: trae a la tabla los costos que solo estaban en
    costos.json (o en la columna costo de Stock) y regenera el espejo.
    Corre una sola vez; después la tabla es la única fuente."""
    if db_load(MARCA_MIGRACION):
        return None
    from modules.contabilidad_cierre import importar_costos_del_sistema
    # Productos con costo distinto en las dos bases: gana la tabla contable
    # (lo decidido), pero queda registro para revisarlos.
    viejo = db_load(ESPEJO) or {}
    tabla = vigentes()
    distintos = [{'item_id': iid, 'titulo': (v or {}).get('titulo') or tabla[iid]['titulo'],
                  'costo_json': float((v or {}).get('costo') or 0), 'costo_tabla': tabla[iid]['costo']}
                 for iid, v in viejo.items()
                 if iid in tabla and float((v or {}).get('costo') or 0) not in (0.0, tabla[iid]['costo'])
                 and tabla[iid]['origen'] != 'costos_json']
    res = importar_costos_del_sistema()
    if 'error' in res:
        return res
    res['espejo'] = sincronizar_espejo()
    res['distintos'] = distintos
    db_save(MARCA_MIGRACION, {'fecha': datetime.now().isoformat(timespec='seconds'), **res})
    return res


def _asegurar_migracion():
    """Antes de escribir: si los costos viejos todavía no pasaron a la tabla,
    pasarlos. Si eso falla, no se escribe nada: regenerar el espejo sin
    migrar borraría los costos que solo estaban en costos.json."""
    res = unificar_una_vez()
    if res and 'error' in res:
        raise RuntimeError(f"No se pudieron unificar los costos: {res['error']}")


MARCA_REPARACION = os.path.join(RAIZ, 'data', '_costos_reparacion_miles.json')


def reparar_miles_una_vez() -> dict | None:
    """Repara los costos de Contabilidad leídos divididos por mil.

    El lector de planillas tomaba '15.000' como 15 (ya corregido en
    contabilidad_cierre._a_decimal). La unificación detectó 39 productos con
    la carga rápida = tabla × 1000. Para cada uno:
      - coincide exacto: todas sus filas < 1000 se multiplican por 1000;
      - no coincide (tabla×1000 ≠ carga rápida): se corrige la historia ×1000
        y el costo de la carga rápida (el que mostraban las pantallas) queda
        vigente desde hoy; se listan para que el usuario confirme.
    Después recalcula el CMV del año. Otros costos < 1000 solo se listan.
    """
    if db_load(MARCA_REPARACION):
        return None
    unif = db_load(MARCA_MIGRACION) or {}
    distintos = unif.get('distintos') or []
    session_scope, Costo = _modelo()
    reparados, revisar = [], []
    with session_scope() as s:
        for d in distintos:
            tabla, rapida = float(d['costo_tabla']), float(d['costo_json'])
            if not (0 < tabla < 1000):
                continue
            # La tabla guarda 2 decimales: '2.673' quedó 2.67 → ×1000 da 2670.
            # A menos de $10 de la carga rápida es el mismo costo: se usa el exacto.
            filas = s.query(Costo).filter(Costo.item_id == d['item_id'], Costo.costo_unitario < 1000).all()
            for f in filas:
                x1000 = Decimal(str(f.costo_unitario)) * 1000
                f.costo_unitario = Decimal(str(rapida)) if abs(float(x1000) - rapida) <= 10 else x1000
                f.notas = ((f.notas or '') + ' | ×1000: corregido separador de miles (08/10/2026)').strip(' |')
            if abs(tabla * 1000 - rapida) <= 10:
                reparados.append(d['item_id'])
            else:
                revisar.append({**d, 'costo_tabla_corregido': tabla * 1000})
        s.flush()   # la sesión no tiene autoflush: sin esto la consulta ve los valores viejos
        sospechosos = [{'item_id': f.item_id, 'titulo': f.titulo, 'costo': float(f.costo_unitario),
                        'vigente_desde': f.vigente_desde.isoformat(), 'origen': f.origen_dato}
                       for f in s.query(Costo).filter(Costo.costo_unitario < 1000).all()]
    for r in revisar:
        guardar(r['item_id'], r['costo_json'], titulo=r.get('titulo'), origen='panel')
    sincronizar_espejo()
    cmv = None
    try:
        from modules.contabilidad_cierre import aplicar_cmv
        hoy = date.today()
        cmv = aplicar_cmv(date(hoy.year, 1, 1), hoy)
        cmv = {k: cmv.get(k) for k in ('generados', 'actualizados', 'sin_costo')}
    except Exception as e:
        cmv = {'error': str(e)}
    res = {'fecha': datetime.now().isoformat(timespec='seconds'), 'reparados': reparados,
           'revisar': revisar, 'sospechosos': sospechosos, 'cmv': cmv}
    db_save(MARCA_REPARACION, res)
    return res


# Costos confirmados por el usuario: reemplazan toda la historia del ítem.
# Guille confirmó el 08/10/2026 que los Cortadores cuestan $13.000 (la
# planilla decía $22.000).
CONFIRMADOS = {
    'MLA1932975847': 13000,
    'MLA2570796766': 13000,
}
MARCA_CONFIRMADOS = os.path.join(RAIZ, 'data', '_costos_confirmados.json')


def aplicar_confirmados_una_vez() -> dict | None:
    """Pone el costo confirmado en todas las vigencias del ítem y recalcula el
    CMV del año. Una sola vez por lista (la marca guarda qué se aplicó)."""
    hecho = db_load(MARCA_CONFIRMADOS) or {}
    pendientes = {k: v for k, v in CONFIRMADOS.items() if hecho.get(k) != v}
    if not pendientes:
        return None
    session_scope, Costo = _modelo()
    with session_scope() as s:
        for iid, valor in pendientes.items():
            for f in s.query(Costo).filter_by(item_id=iid).all():
                f.costo_unitario = Decimal(str(valor))
                f.notas = ((f.notas or '') + f' | confirmado por el usuario: ${valor} (08/10/2026)').strip(' |')
    sincronizar_espejo()
    try:
        from modules.contabilidad_cierre import aplicar_cmv
        hoy = date.today()
        aplicar_cmv(date(hoy.year, 1, 1), hoy)
    except Exception:
        pass
    hecho.update(pendientes)
    db_save(MARCA_CONFIRMADOS, hecho)
    return pendientes
