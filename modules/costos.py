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
