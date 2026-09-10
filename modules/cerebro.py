"""
Cerebro — capa de aprendizaje del Sistema ML (Sprint A).

Cierra el loop del sistema: observar -> proponer -> (aprobar) -> ejecutar ->
REGISTRAR -> EVALUAR a 7/14 dias -> APRENDIZAJES.

Tres registros por cuenta, todos en JSON via core.db_storage (Regla #5: no
Postgres relacional, kv_store + filesystem local):

    data/cerebro_<Alias>/acciones.json      -> una entrada por cambio (1.1/1.2)
    data/cerebro_<Alias>/aprendizajes.json  -> reglas consolidadas (1.3)
    data/cerebro_<Alias>/competidores.json  -> competidores por publicacion (2.x)
    data/cerebro_<Alias>/snapshots.json     -> serie diaria por item (1.4/4.1)

Diseno documentado en docs/CEREBRO.md. Este modulo NO llama a la API de ML:
recibe la data ya cargada. Eso lo hace testeable y barato.

Sin dependencias de Claude: la evaluacion es estadistica pura.
"""

from __future__ import annotations

import logging
import math
import os
import re
import uuid
from datetime import datetime, timedelta

from core.db_storage import db_load, db_save

_logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(__file__), '..', 'data')

# ── Constantes de dominio ────────────────────────────────────────────────────

# Estados del ciclo de vida de una accion (1.1)
ESTADO_PENDIENTE   = 'pendiente'
ESTADO_APROBADA    = 'aprobada'
ESTADO_RECHAZADA   = 'rechazada'
ESTADO_APLICADA    = 'aplicada'
ESTADO_EVALUADA_7  = 'evaluada_7d'
ESTADO_EVALUADA_14 = 'evaluada_14d'

ESTADOS_VALIDOS = {
    ESTADO_PENDIENTE, ESTADO_APROBADA, ESTADO_RECHAZADA,
    ESTADO_APLICADA, ESTADO_EVALUADA_7, ESTADO_EVALUADA_14,
}

# Tipos de accion registrables
TIPOS_ACCION = {
    'precio', 'descripcion', 'ficha', 'respuesta', 'pausa', 'clon',
    'publicacion_nueva', 'ads_presupuesto', 'postura', 'promocion', 'cupon',
}

# Origen de la accion
ORIGEN_AUTO      = 'sistema_auto'
ORIGEN_PROPUESTO = 'sistema_propuesto'
ORIGEN_USUARIO   = 'usuario'

# Cuantos dias vale una propuesta como explicacion de un cambio aplicado. Mas
# alla de eso, el cambio de hoy no lo explica una sugerencia de hace un mes.
DIAS_PARA_ADOPTAR_PROPUESTA = 14

# Veredictos de la evaluacion (1.2)
VEREDICTO_FUNCIONO   = 'funciono'
VEREDICTO_NEUTRA     = 'neutra'
VEREDICTO_EMPEORO    = 'empeoro'
VEREDICTO_CONTAMINADA = 'contaminada'
VEREDICTO_SIN_DATOS  = 'sin_datos'

# Clases de competidor (2.2)
CLASE_DIRECTO    = 'directo'
CLASE_SUSTITUTO  = 'sustituto'
CLASE_RUIDO      = 'ruido'
CLASE_CANDIDATO  = 'candidato'

# Estados de stock inferidos del competidor (2.4)
STOCK_NORMAL      = 'normal'
STOCK_POCO        = 'poco_stock'
STOCK_SIN         = 'sin_stock'
STOCK_LIQUIDANDO  = 'liquidando'

# Retencion de la serie de snapshots (1.4)
RETENCION_SNAPSHOTS_DIAS = 180

# Ventanas de evaluacion
VENTANAS_EVALUACION = (7, 14)

# Puntaje minimo para que un candidato llegue a la bandeja (2.2.5)
PUNTAJE_MIN_CANDIDATO = 0.5
# Puntaje minimo para usar un competidor en precio SIN confirmacion del usuario
# y ademas exige mismo catalog_product_id (2.2.6, modo cauteloso)
PUNTAJE_MIN_AUTO_DIRECTO = 0.85

# Confianza de un aprendizaje segun cantidad de casos (1.3)
_CONFIANZA_MEDIA_DESDE = 3
_CONFIANZA_ALTA_DESDE  = 10


# ── Helpers de path y persistencia ───────────────────────────────────────────

def _safe(alias: str) -> str:
    return (alias or '').replace(' ', '_').replace('/', '-')


def _dir(alias: str) -> str:
    return os.path.join(DATA_DIR, f'cerebro_{_safe(alias)}')


def _path(alias: str, nombre: str) -> str:
    return os.path.join(_dir(alias), nombre)


def _load(alias: str, nombre: str, default):
    try:
        data = db_load(_path(alias, nombre))
    except Exception as e:
        _logger.warning('[cerebro] load %s/%s fallo: %s', alias, nombre, e)
        return default
    return data if data is not None else default


def _save(alias: str, nombre: str, data) -> bool:
    try:
        db_save(_path(alias, nombre), data)
        return True
    except Exception as e:
        _logger.error('[cerebro] save %s/%s fallo: %s', alias, nombre, e)
        return False


def _hoy() -> str:
    return datetime.now().strftime('%Y-%m-%d')


def _ahora() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _fecha(valor) -> datetime | None:
    """Parsea 'YYYY-MM-DD' o 'YYYY-MM-DD HH:MM[:SS]' de forma tolerante."""
    if not valor:
        return None
    if isinstance(valor, datetime):
        return valor
    txt = str(valor).strip().replace('T', ' ')
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return datetime.strptime(txt[:len(datetime.now().strftime(fmt))], fmt)
        except ValueError:
            continue
    return None


def _dia(valor) -> str:
    d = _fecha(valor)
    return d.strftime('%Y-%m-%d') if d else ''


def _num(valor, default=0.0) -> float:
    try:
        if valor is None or valor == '':
            return default
        return float(valor)
    except (TypeError, ValueError):
        return default


# ══════════════════════════════════════════════════════════════════════════════
# 1.1 — Registro de acciones
# ══════════════════════════════════════════════════════════════════════════════

def _acciones_raw(alias: str) -> dict:
    data = _load(alias, 'acciones.json', {'acciones': []})
    if not isinstance(data, dict):
        data = {'acciones': []}
    data.setdefault('acciones', [])
    return data


def listar_acciones(alias: str, estado: str | None = None, tipo: str | None = None,
                    item_id: str | None = None, desde: str | None = None,
                    limit: int | None = None) -> list[dict]:
    """Devuelve acciones filtradas, mas recientes primero."""
    accs = _acciones_raw(alias)['acciones']
    out = []
    for a in accs:
        if estado and a.get('estado') != estado:
            continue
        if tipo and a.get('tipo') != tipo:
            continue
        if item_id and a.get('item_id') != item_id:
            continue
        if desde and _dia(a.get('ts')) < desde:
            continue
        out.append(a)
    out.sort(key=lambda x: x.get('ts', ''), reverse=True)
    return out[:limit] if limit else out


def get_accion(alias: str, accion_id: str) -> dict | None:
    for a in _acciones_raw(alias)['acciones']:
        if a.get('id') == accion_id:
            return a
    return None


def registrar_accion(alias: str, *, tipo: str, item_id: str,
                     origen: str = ORIGEN_AUTO,
                     hipotesis: str = '',
                     estado_previo: dict | None = None,
                     detalle: dict | None = None,
                     estado: str | None = None,
                     ejecutado_por: str = '') -> dict:
    """Registra una accion ejecutada o propuesta. Devuelve la accion creada.

    Nunca lanza: si la persistencia falla, loguea y devuelve la accion igual.
    Registrar no puede romper el flujo que lo llama.
    """
    if tipo not in TIPOS_ACCION:
        _logger.warning('[cerebro] tipo de accion desconocido: %s', tipo)

    if estado is None:
        estado = ESTADO_PENDIENTE if origen == ORIGEN_PROPUESTO else ESTADO_APLICADA

    accion = {
        'id':            uuid.uuid4().hex[:12],
        'ts':            _ahora(),
        'item_id':       item_id,
        'tipo':          tipo,
        'origen':        origen,
        'hipotesis':     (hipotesis or '')[:400],
        'estado_previo': estado_previo or {},
        'detalle':       detalle or {},
        'estado':        estado,
        'ejecutado_por': ejecutado_por,
        'evaluacion':    {},
    }
    if estado == ESTADO_APLICADA:
        accion['aplicada_ts'] = accion['ts']

    data = _acciones_raw(alias)
    data['acciones'].append(accion)
    # Rolling defensivo: 5000 acciones alcanzan para anios con ~70 publicaciones
    if len(data['acciones']) > 5000:
        data['acciones'] = data['acciones'][-5000:]
    _save(alias, 'acciones.json', data)

    # Una propuesta que nadie ve no sirve de nada: avisa apenas nace. Se hace
    # aca y no en cada llamador para que ninguna propuesta futura quede muda por
    # olvido. El sandbox del backtest queda afuera.
    if estado == ESTADO_PENDIENTE and not alias.startswith('_backtest'):
        try:
            from modules import telegram_bot
            telegram_bot.notificar_propuesta(alias, accion)
        except Exception as e:
            _logger.warning('[cerebro] aviso de propuesta fallo: %s', e)

    return accion


def actualizar_accion(alias: str, accion_id: str, **campos) -> dict | None:
    """Actualiza campos de una accion existente. Devuelve la accion o None."""
    data = _acciones_raw(alias)
    for a in data['acciones']:
        if a.get('id') != accion_id:
            continue
        nuevo_estado = campos.get('estado')
        if nuevo_estado and nuevo_estado not in ESTADOS_VALIDOS:
            _logger.warning('[cerebro] estado invalido: %s', nuevo_estado)
            campos.pop('estado')
            nuevo_estado = None
        a.update(campos)
        if nuevo_estado == ESTADO_APLICADA and not a.get('aplicada_ts'):
            a['aplicada_ts'] = _ahora()
        _save(alias, 'acciones.json', data)
        return a
    return None


def aprobar_accion(alias: str, accion_id: str, por: str = 'usuario') -> dict | None:
    """Aprueba una propuesta. El precio arranca siempre en propone-y-apruebo (3.5)."""
    return actualizar_accion(alias, accion_id, estado=ESTADO_APROBADA,
                             aprobada_ts=_ahora(), aprobada_por=por)


def rechazar_accion(alias: str, accion_id: str, por: str = 'usuario',
                    motivo: str = '') -> dict | None:
    return actualizar_accion(alias, accion_id, estado=ESTADO_RECHAZADA,
                             rechazada_ts=_ahora(), rechazada_por=por,
                             motivo_rechazo=motivo[:300])


def marcar_aplicada(alias: str, accion_id: str, detalle_final: dict | None = None) -> dict | None:
    campos = {'estado': ESTADO_APLICADA, 'aplicada_ts': _ahora()}
    if detalle_final:
        acc = get_accion(alias, accion_id) or {}
        det = dict(acc.get('detalle') or {})
        det.update(detalle_final)
        campos['detalle'] = det
    return actualizar_accion(alias, accion_id, **campos)


def aplicar_o_registrar(alias: str, *, tipo: str, item_id: str, **kwargs) -> dict:
    """Cierra el ciclo propone -> aplica en vez de dejar la propuesta huerfana.

    Los modulos que proponen (defensa_publicacion, competencia_diagnostico)
    dejan la accion en `pendiente`. Cuando el usuario despues aplica ese cambio,
    el flujo que lo ejecuta registraba una accion NUEVA: la propuesta quedaba
    pendiente para siempre y nunca se evaluaba, que es justo la parte por la que
    existe Cerebro. Ahora, si hay una propuesta pendiente del mismo tipo para la
    misma publicacion, se la marca aplicada y se le pega el detalle real de lo
    que se hizo — asi la hipotesis que la origino se puede contrastar contra el
    resultado.

    Solo se levanta una propuesta reciente: una de hace un mes no explica un
    cambio de hoy.
    """
    desde = (datetime.now() - timedelta(days=DIAS_PARA_ADOPTAR_PROPUESTA)).strftime('%Y-%m-%d')
    pendientes = [a for a in listar_acciones(alias, estado=ESTADO_PENDIENTE,
                                             tipo=tipo, item_id=item_id, desde=desde)
                  if a.get('origen') == ORIGEN_PROPUESTO]
    if pendientes:
        prop = pendientes[0]
        detalle = dict(kwargs.get('detalle') or {})
        detalle['adoptada_de_propuesta'] = True
        if kwargs.get('ejecutado_por'):
            detalle['ejecutado_por'] = kwargs['ejecutado_por']
        actualizada = marcar_aplicada(alias, prop['id'], detalle)
        if actualizada:
            return actualizada
    return registrar_accion(alias, tipo=tipo, item_id=item_id, **kwargs)


def acciones_pendientes(alias: str) -> list[dict]:
    """Bandeja: propuestas esperando decision del usuario (bloque 7)."""
    return listar_acciones(alias, estado=ESTADO_PENDIENTE)


# ══════════════════════════════════════════════════════════════════════════════
# 1.4 — Snapshots enriquecidos (visitas organicas vs Ads)
# ══════════════════════════════════════════════════════════════════════════════

def _snapshots_raw(alias: str) -> dict:
    data = _load(alias, 'snapshots.json', {'items': {}})
    if not isinstance(data, dict):
        data = {'items': {}}
    data.setdefault('items', {})
    return data


def construir_snapshot(*, fecha: str | None = None,
                       visitas_totales: float | None = None,
                       clics_ads: float | None = None,
                       ads: dict | None = None,
                       unidades: float | None = None,
                       precio: float | None = None,
                       stock: float | None = None,
                       posicion: int | None = None,
                       posiciones_kw: dict | None = None,
                       rating: float | None = None,
                       preguntas_sin_responder: int | None = None,
                       status: str | None = None,
                       categoria: str | None = None,
                       extra: dict | None = None) -> dict:
    """Arma un snapshot diario ya normalizado.

    Separa trafico pago de organico (4.1): las visitas de una publicacion
    pautada no son organicas, y atribuir a la ficha lo que compro la pauta es
    la forma mas facil de aprender una mentira.
    """
    vis_tot = _num(visitas_totales, 0.0)
    clics   = _num(clics_ads, 0.0)
    organicas = max(vis_tot - clics, 0.0)
    uds = _num(unidades, 0.0)

    snap = {
        'fecha':            fecha or _hoy(),
        'visitas_totales':  round(vis_tot, 2),
        'clics_ads':        round(clics, 2),
        'visitas_organicas': round(organicas, 2),
        'unidades':         round(uds, 2),
        'precio':           _num(precio, 0.0) or None,
        'stock':            stock,
        'posicion':         posicion,
        'rating':           rating,
        'status':           status,
        'categoria':        categoria,
    }
    # Conversion organica y conversion Ads por separado (4.1)
    snap['conv_organica'] = round(uds / organicas * 100, 3) if organicas > 0 else None
    if ads:
        snap['ads'] = {
            'impresiones': _num(ads.get('impresiones')),
            'clics':       clics,
            'costo':       _num(ads.get('costo')),
            'ventas':      _num(ads.get('ventas')),
            'acos':        ads.get('acos'),
        }
        uds_ads = _num(ads.get('ventas'))
        snap['conv_ads'] = round(uds_ads / clics * 100, 3) if clics > 0 else None
    if posiciones_kw:
        snap['posiciones_kw'] = posiciones_kw
    if preguntas_sin_responder is not None:
        snap['preguntas_sin_responder'] = preguntas_sin_responder
    if extra:
        snap.update(extra)
    return snap


def guardar_snapshot(alias: str, item_id: str, snap: dict,
                     competidores: list[dict] | None = None) -> None:
    """Agrega (o reemplaza) el snapshot del dia para un item. Retencion 180 dias."""
    if not item_id:
        return
    data = _snapshots_raw(alias)
    entry = data['items'].setdefault(item_id, {'serie': []})
    dia = _dia(snap.get('fecha')) or _hoy()
    snap = dict(snap)
    snap['fecha'] = dia
    if competidores is not None:
        # Por competidor directo confirmado: precio, cantidad, status, posicion (1.4)
        snap['competidores'] = competidores

    serie = [s for s in entry.get('serie', []) if _dia(s.get('fecha')) != dia]
    serie.append(snap)
    serie.sort(key=lambda s: _dia(s.get('fecha')))

    corte = (datetime.now() - timedelta(days=RETENCION_SNAPSHOTS_DIAS)).strftime('%Y-%m-%d')
    entry['serie'] = [s for s in serie if _dia(s.get('fecha')) >= corte]
    entry['ultimo'] = entry['serie'][-1] if entry['serie'] else None
    data['items'][item_id] = entry
    _save(alias, 'snapshots.json', data)


def guardar_snapshots_batch(alias: str, snaps_por_item: dict[str, dict]) -> int:
    """Guarda muchos snapshots con UNA sola escritura (evita N writes al kv_store)."""
    if not snaps_por_item:
        return 0
    data = _snapshots_raw(alias)
    corte = (datetime.now() - timedelta(days=RETENCION_SNAPSHOTS_DIAS)).strftime('%Y-%m-%d')
    guardados = 0
    for item_id, snap in snaps_por_item.items():
        if not item_id or not snap:
            continue
        entry = data['items'].setdefault(item_id, {'serie': []})
        dia = _dia(snap.get('fecha')) or _hoy()
        snap = dict(snap)
        snap['fecha'] = dia
        serie = [s for s in entry.get('serie', []) if _dia(s.get('fecha')) != dia]
        serie.append(snap)
        serie.sort(key=lambda s: _dia(s.get('fecha')))
        entry['serie'] = [s for s in serie if _dia(s.get('fecha')) >= corte]
        entry['ultimo'] = entry['serie'][-1] if entry['serie'] else None
        data['items'][item_id] = entry
        guardados += 1
    _save(alias, 'snapshots.json', data)
    return guardados


def serie_item(alias: str, item_id: str, desde: str | None = None,
               hasta: str | None = None) -> list[dict]:
    entry = _snapshots_raw(alias)['items'].get(item_id) or {}
    serie = entry.get('serie', [])
    if desde:
        serie = [s for s in serie if _dia(s.get('fecha')) >= desde]
    if hasta:
        serie = [s for s in serie if _dia(s.get('fecha')) <= hasta]
    return serie


def ultimo_snapshot(alias: str, item_id: str) -> dict:
    entry = _snapshots_raw(alias)['items'].get(item_id) or {}
    return entry.get('ultimo') or {}


def items_con_serie(alias: str) -> list[str]:
    return list(_snapshots_raw(alias)['items'].keys())


def capturar_estado_previo(alias: str, item_id: str, extra: dict | None = None) -> dict:
    """Snapshot del item al momento de la accion (campo estado_previo de 1.1).

    Usa lo ultimo que Cerebro ya sabe; el llamador puede pisar/completar con
    `extra` sin tener que pegarle de nuevo a la API.
    """
    ult = ultimo_snapshot(alias, item_id)
    serie7 = serie_item(alias, item_id,
                        desde=(datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d'))
    previo = {
        'fecha':             _ahora(),
        'precio':            ult.get('precio'),
        'stock':             ult.get('stock'),
        'posicion':          ult.get('posicion'),
        'rating':            ult.get('rating'),
        'visitas_7d':        round(sum(_num(s.get('visitas_organicas')) for s in serie7), 2) or None,
        'unidades_7d':       round(sum(_num(s.get('unidades')) for s in serie7), 2) or None,
        'conversion_7d':     None,
        'dias_con_serie':    len(serie7),
    }
    vis = _num(previo['visitas_7d'])
    if vis > 0:
        previo['conversion_7d'] = round(_num(previo['unidades_7d']) / vis * 100, 3)
    if extra:
        previo.update({k: v for k, v in extra.items() if v is not None})
    return previo


# ══════════════════════════════════════════════════════════════════════════════
# 1.2 — Evaluacion a 7 y 14 dias
# ══════════════════════════════════════════════════════════════════════════════

# Metricas que se comparan, con su direccion deseada (+1 = mas es mejor)
_METRICAS = {
    'visitas_organicas_dia': +1,
    'conversion_organica':   +1,
    'unidades_dia':          +1,
    'posicion':              -1,   # menos es mejor
}

# Un movimiento tiene que superar este porcentaje Y el ruido historico
_UMBRAL_MINIMO_PCT = 5.0


def _metricas_ventana(serie: list[dict]) -> dict:
    """Promedios diarios de una ventana de snapshots."""
    if not serie:
        return {}
    dias = len(serie)
    vis = sum(_num(s.get('visitas_organicas')) for s in serie)
    uds = sum(_num(s.get('unidades')) for s in serie)
    posiciones = [s['posicion'] for s in serie
                  if s.get('posicion') is not None and s.get('posicion') != 999]
    return {
        'dias':                   dias,
        'visitas_organicas_dia':  round(vis / dias, 3),
        'unidades_dia':           round(uds / dias, 4),
        'conversion_organica':    round(uds / vis * 100, 3) if vis > 0 else 0.0,
        'posicion':               round(sum(posiciones) / len(posiciones), 2) if posiciones else None,
        'precio_medio':           round(
            sum(_num(s.get('precio')) for s in serie if s.get('precio')) /
            max(len([s for s in serie if s.get('precio')]), 1), 2) or None,
    }


def _ruido_historico(serie: list[dict], metrica: str) -> float:
    """Desvio estandar relativo (%) de la metrica dia a dia.

    Es el piso de significancia: un movimiento por debajo del ruido propio de
    la publicacion no es una senal, es la publicacion respirando.
    """
    valores = []
    for s in serie:
        if metrica == 'visitas_organicas_dia':
            valores.append(_num(s.get('visitas_organicas')))
        elif metrica == 'unidades_dia':
            valores.append(_num(s.get('unidades')))
        elif metrica == 'conversion_organica':
            v = s.get('conv_organica')
            if v is not None:
                valores.append(_num(v))
        elif metrica == 'posicion':
            if s.get('posicion') not in (None, 999):
                valores.append(_num(s.get('posicion')))
    valores = [v for v in valores if v is not None]
    if len(valores) < 4:
        return 25.0   # sin historia suficiente, exigimos un movimiento grande
    media = sum(valores) / len(valores)
    if media == 0:
        return 25.0
    var = sum((v - media) ** 2 for v in valores) / (len(valores) - 1)
    return min(round(math.sqrt(var) / abs(media) * 100, 2), 100.0)


def _delta_pct(antes: float | None, despues: float | None) -> float | None:
    if antes in (None, 0) or despues is None:
        return None
    return round((despues - antes) / abs(antes) * 100, 2)


def _hermanas_control(alias: str, item_id: str, categoria: str | None,
                      desde: str, hasta: str, acciones: list[dict]) -> tuple[float | None, int]:
    """Movimiento medio de las publicaciones hermanas NO tocadas en la ventana.

    Control del bloque 1.2: si el mercado entero cayo 30%, mantenerse es ganar.
    Devuelve (delta_pct_medio_de_visitas_organicas, cantidad_de_hermanas).
    """
    tocados = {a.get('item_id') for a in acciones
               if a.get('item_id') != item_id
               and desde <= _dia(a.get('aplicada_ts') or a.get('ts')) <= hasta}

    deltas = []
    for otro in items_con_serie(alias):
        if otro == item_id or otro in tocados:
            continue
        serie_post = serie_item(alias, otro, desde=desde, hasta=hasta)
        if not serie_post:
            continue
        if categoria:
            cats = {s.get('categoria') for s in serie_post if s.get('categoria')}
            if cats and categoria not in cats:
                continue
        dias = len(serie_post)
        d_desde = (_fecha(desde) - timedelta(days=dias)).strftime('%Y-%m-%d')
        d_hasta = (_fecha(desde) - timedelta(days=1)).strftime('%Y-%m-%d')
        serie_pre = serie_item(alias, otro, desde=d_desde, hasta=d_hasta)
        if not serie_pre:
            continue
        m_pre  = _metricas_ventana(serie_pre)
        m_post = _metricas_ventana(serie_post)
        d = _delta_pct(m_pre.get('visitas_organicas_dia'), m_post.get('visitas_organicas_dia'))
        if d is not None:
            deltas.append(d)

    if not deltas:
        return None, 0
    return round(sum(deltas) / len(deltas), 2), len(deltas)


def _hubo_otra_accion(alias: str, item_id: str, accion_id: str,
                      desde: str, hasta: str, acciones: list[dict]) -> list[str]:
    """Contaminacion (4.1): otra accion sobre el mismo item dentro de la ventana."""
    otras = []
    for a in acciones:
        if a.get('item_id') != item_id or a.get('id') == accion_id:
            continue
        if a.get('estado') not in (ESTADO_APLICADA, ESTADO_EVALUADA_7, ESTADO_EVALUADA_14):
            continue
        dia = _dia(a.get('aplicada_ts') or a.get('ts'))
        if desde <= dia <= hasta:
            otras.append(f"{a.get('tipo')}@{dia}")
    return otras


def evaluar_accion(alias: str, accion: dict, ventana: int,
                   acciones: list[dict] | None = None,
                   metrica_forzada: str | None = None) -> dict:
    """Evalua una accion a N dias contra control. No escribe: devuelve el dict.

    `metrica_forzada` fija cual manda el veredicto en vez de elegirla sola. Lo
    usa el backtest: sobre el historico reconstruido, las unidades previas son
    una estimacion del baseline y las posteriores salen de diferencias de
    ventas acumuladas, asi que compararlas seria mezclar dos cosas distintas y
    el resultado se iria sistematicamente hacia "empeoro". Las visitas, en
    cambio, vienen de la misma fuente en las dos ventanas.
    """
    item_id = accion.get('item_id')
    base = _fecha(accion.get('aplicada_ts') or accion.get('ts'))
    if not item_id or not base:
        return {'veredicto': VEREDICTO_SIN_DATOS, 'motivo': 'accion sin item o sin fecha'}

    acciones = acciones if acciones is not None else _acciones_raw(alias)['acciones']

    post_desde = (base + timedelta(days=1)).strftime('%Y-%m-%d')
    post_hasta = (base + timedelta(days=ventana)).strftime('%Y-%m-%d')
    pre_desde  = (base - timedelta(days=ventana)).strftime('%Y-%m-%d')
    pre_hasta  = (base - timedelta(days=1)).strftime('%Y-%m-%d')

    serie_pre  = serie_item(alias, item_id, desde=pre_desde,  hasta=pre_hasta)
    serie_post = serie_item(alias, item_id, desde=post_desde, hasta=post_hasta)

    minimo = max(3, ventana // 3)
    if len(serie_pre) < minimo or len(serie_post) < minimo:
        return {
            'veredicto': VEREDICTO_SIN_DATOS,
            'ventana_dias': ventana,
            'motivo': f'serie insuficiente (pre={len(serie_pre)}, post={len(serie_post)}, '
                      f'minimo={minimo})',
            'evaluada_ts': _ahora(),
        }

    # Contaminacion (4.1): otra accion sobre el mismo item cerca de esta.
    # Se mira la ventana COMPLETA (antes y despues), no solo la posterior: una
    # accion que cae en la ventana previa mueve la linea de base contra la que
    # se compara, y el efecto que midamos va a ser de las dos juntas. Lo
    # encontro un test que esperaba que dos acciones encimadas no ensenaran
    # nada y descubrio que la segunda igual generaba un aprendizaje.
    contaminada = _hubo_otra_accion(alias, item_id, accion.get('id'),
                                    pre_desde, post_hasta, acciones)

    m_pre  = _metricas_ventana(serie_pre)
    m_post = _metricas_ventana(serie_post)

    categoria = None
    for s in reversed(serie_pre):
        if s.get('categoria'):
            categoria = s['categoria']
            break

    control_delta, control_n = _hermanas_control(alias, item_id, categoria,
                                                 post_desde, post_hasta, acciones)
    control_tipo = 'hermanas'
    if control_delta is None:
        # Sin hermanas: control = la propia publicacion en los 14 dias previos
        largo = max(ventana * 2, 14)
        c_desde = (base - timedelta(days=largo)).strftime('%Y-%m-%d')
        c_hasta = (base - timedelta(days=ventana + 1)).strftime('%Y-%m-%d')
        serie_c = serie_item(alias, item_id, desde=c_desde, hasta=c_hasta)
        if serie_c:
            m_c = _metricas_ventana(serie_c)
            control_delta = _delta_pct(m_c.get('visitas_organicas_dia'),
                                       m_pre.get('visitas_organicas_dia'))
            control_tipo = 'propia_historica'
    if control_delta is None:
        control_delta = 0.0
        control_tipo  = 'sin_control'

    deltas: dict = {}
    for metrica, direccion in _METRICAS.items():
        d = _delta_pct(m_pre.get(metrica), m_post.get(metrica))
        if d is None:
            continue
        deltas[metrica] = {
            'antes':   m_pre.get(metrica),
            'despues': m_post.get(metrica),
            'delta_pct': d,
            # Descontar el movimiento del mercado/hermanas (4.2)
            'delta_vs_control_pct': round(d - control_delta, 2)
            if metrica == 'visitas_organicas_dia' else d,
            'direccion': direccion,
        }

    # Metrica que manda el veredicto: unidades si hay ventas, si no visitas
    if metrica_forzada and metrica_forzada in deltas:
        principal = metrica_forzada
    else:
        principal = 'unidades_dia' if _num(m_post.get('unidades_dia')) or _num(m_pre.get('unidades_dia')) \
            else 'visitas_organicas_dia'
    detalle_principal = deltas.get(principal) or deltas.get('visitas_organicas_dia') or {}
    efecto = _num(detalle_principal.get('delta_vs_control_pct'), 0.0)
    ruido = _ruido_historico(serie_pre + serie_post, principal)
    umbral = max(ruido, _UMBRAL_MINIMO_PCT)
    direccion = _METRICAS.get(principal, 1)
    efecto_dirigido = efecto * direccion

    if contaminada:
        veredicto = VEREDICTO_CONTAMINADA
    elif efecto_dirigido > umbral:
        veredicto = VEREDICTO_FUNCIONO
    elif efecto_dirigido < -umbral:
        veredicto = VEREDICTO_EMPEORO
    else:
        veredicto = VEREDICTO_NEUTRA

    return {
        'veredicto':       veredicto,
        'ventana_dias':    ventana,
        'metrica_principal': principal,
        'efecto_pct':      round(efecto, 2),
        'magnitud':        round(abs(efecto), 2),
        'umbral_pct':      round(umbral, 2),
        'ruido_pct':       ruido,
        'control':         {'tipo': control_tipo, 'delta_pct': control_delta, 'n': control_n},
        'contaminada_por': contaminada,
        'deltas':          deltas,
        'dias_pre':        len(serie_pre),
        'dias_post':       len(serie_post),
        'evaluada_ts':     _ahora(),
    }


def evaluar_acciones_pendientes(alias: str, hoy: str | None = None,
                                metrica_forzada: str | None = None) -> dict:
    """Job diario `cerebro_evaluar` (04:30, despues del snapshot).

    Busca acciones aplicadas hace 7 y 14 dias y las evalua. Devuelve resumen.
    """
    data = _acciones_raw(alias)
    acciones = data['acciones']
    ref = _fecha(hoy) or datetime.now()
    resumen = {'alias': alias, 'evaluadas_7d': 0, 'evaluadas_14d': 0,
               'sin_datos': 0, 'contaminadas': 0, 'veredictos': {}}
    cambios = False

    for a in acciones:
        if a.get('estado') not in (ESTADO_APLICADA, ESTADO_EVALUADA_7):
            continue
        base = _fecha(a.get('aplicada_ts') or a.get('ts'))
        if not base:
            continue
        dias = (ref.date() - base.date()).days

        objetivo = None
        if a['estado'] == ESTADO_APLICADA and dias >= 7:
            objetivo = 7
        elif a['estado'] == ESTADO_EVALUADA_7 and dias >= 14:
            objetivo = 14
        if objetivo is None:
            continue

        ev = evaluar_accion(alias, a, objetivo, acciones=acciones,
                            metrica_forzada=metrica_forzada)
        evs = a.get('evaluacion') or {}
        if not isinstance(evs, dict):
            evs = {}
        evs[f'{objetivo}d'] = ev
        a['evaluacion'] = evs

        if ev['veredicto'] == VEREDICTO_SIN_DATOS:
            # No avanzamos de estado: puede haber datos manana
            resumen['sin_datos'] += 1
            cambios = True
            continue

        a['estado'] = ESTADO_EVALUADA_7 if objetivo == 7 else ESTADO_EVALUADA_14
        resumen[f'evaluadas_{objetivo}d'] += 1
        resumen['veredictos'][ev['veredicto']] = resumen['veredictos'].get(ev['veredicto'], 0) + 1
        if ev['veredicto'] == VEREDICTO_CONTAMINADA:
            resumen['contaminadas'] += 1
        cambios = True

    if cambios:
        _save(alias, 'acciones.json', data)
        recalcular_aprendizajes(alias)

    return resumen


# ══════════════════════════════════════════════════════════════════════════════
# 1.3 — Aprendizajes consolidados
# ══════════════════════════════════════════════════════════════════════════════

def _clave_aprendizaje(accion: dict) -> tuple[str, str, str]:
    """Devuelve (clave, tipo_accion, segmento)."""
    tipo = accion.get('tipo', 'desconocido')
    det  = accion.get('detalle') or {}
    sub  = ''
    if tipo == 'precio':
        antes   = _num(det.get('precio_antes'))
        despues = _num(det.get('precio_despues'))
        if antes and despues:
            sub = 'bajar' if despues < antes else ('subir' if despues > antes else '')
    tipo_accion = f'{tipo}.{sub}' if sub else tipo

    cat = accion.get('estado_previo', {}).get('categoria') or det.get('categoria')
    segmento = f'categoria:{cat}' if cat else 'general'
    return f'{tipo_accion}.{segmento}', tipo_accion, segmento


def _confianza(casos: int) -> str:
    if casos >= _CONFIANZA_ALTA_DESDE:
        return 'alta'
    if casos >= _CONFIANZA_MEDIA_DESDE:
        return 'media'
    return 'baja'


def recalcular_aprendizajes(alias: str) -> dict:
    """Reescribe aprendizajes.json a partir de todas las evaluaciones cerradas.

    Las contaminadas NO alimentan: mejor "no se" que aprender una mentira (4.1).
    """
    acciones = _acciones_raw(alias)['acciones']
    acum: dict[str, dict] = {}

    for a in acciones:
        evs = a.get('evaluacion') or {}
        if not isinstance(evs, dict):
            continue
        ev = evs.get('14d') or evs.get('7d')
        if not ev:
            continue
        veredicto = ev.get('veredicto')
        if veredicto in (VEREDICTO_CONTAMINADA, VEREDICTO_SIN_DATOS, None):
            continue

        clave, tipo_accion, segmento = _clave_aprendizaje(a)
        e = acum.setdefault(clave, {
            'clave': clave, 'tipo_accion': tipo_accion, 'segmento': segmento,
            'casos': 0, 'funciono': 0, 'neutra': 0, 'empeoro': 0,
            'efecto_acumulado': 0.0, 'ultimo_caso': '',
        })
        e['casos'] += 1
        e[veredicto] = e.get(veredicto, 0) + 1
        e['efecto_acumulado'] += _num(ev.get('efecto_pct'))
        dia = _dia(a.get('aplicada_ts') or a.get('ts'))
        if dia > e['ultimo_caso']:
            e['ultimo_caso'] = dia

    aprendizajes = {}
    for clave, e in acum.items():
        casos = e['casos']
        acierto = round(e['funciono'] / casos, 3) if casos else 0.0
        efecto_medio = round(e['efecto_acumulado'] / casos, 2) if casos else 0.0
        aprendizajes[clave] = {
            'clave':        clave,
            'texto':        _texto_aprendizaje(e, acierto, efecto_medio),
            'tipo_accion':  e['tipo_accion'],
            'segmento':     e['segmento'],
            'casos':        casos,
            'funciono':     e['funciono'],
            'neutra':       e['neutra'],
            'empeoro':      e['empeoro'],
            'acierto':      acierto,
            'efecto_medio_pct': efecto_medio,
            'confianza':    _confianza(casos),
            'ultimo_caso':  e['ultimo_caso'],
            'actualizado':  _hoy(),
        }

    _save(alias, 'aprendizajes.json', {'aprendizajes': aprendizajes,
                                       'actualizado': _ahora()})
    return aprendizajes


def _texto_aprendizaje(e: dict, acierto: float, efecto_medio: float) -> str:
    seg = e['segmento'].replace('categoria:', '') if e['segmento'] != 'general' else 'general'
    accion = e['tipo_accion'].replace('.', ' ')
    if acierto >= 0.7:
        juicio = 'funciono'
    elif acierto <= 0.2 and e['casos'] >= 3:
        juicio = 'no movio la aguja'
    else:
        juicio = 'resultado mixto'
    return (f'En {seg}, {accion} {juicio} '
            f'({e["funciono"]}/{e["casos"]} casos, efecto medio {efecto_medio:+.1f}%).')


def listar_aprendizajes(alias: str) -> dict:
    data = _load(alias, 'aprendizajes.json', {'aprendizajes': {}})
    return (data or {}).get('aprendizajes', {})


def consultar_aprendizaje(alias: str, tipo_accion: str,
                          categoria: str | None = None) -> dict | None:
    """Lo que el sistema ya sabe sobre este tipo de accion en este segmento.

    Lo usan las recomendaciones (bloque 5) para ordenar por historial propio.
    """
    aps = listar_aprendizajes(alias)
    if categoria:
        especifico = aps.get(f'{tipo_accion}.categoria:{categoria}')
        if especifico:
            return especifico
    return aps.get(f'{tipo_accion}.general')


# ══════════════════════════════════════════════════════════════════════════════
# 2 — Competidores persistentes, asociados a una publicacion
# ══════════════════════════════════════════════════════════════════════════════

_STOP_WORDS = {
    'de', 'la', 'el', 'para', 'con', 'sin', 'por', 'y', 'a', 'en', 'un', 'una',
    'los', 'las', 'del', 'al', 'x', 'mas', 'pack', 'kit', 'set', 'nuevo', 'nueva',
    'original', 'envio', 'gratis', 'oferta', 'promo',
}


def _tokens(texto: str) -> set[str]:
    palabras = re.findall(r'[a-z0-9]+', (texto or '').lower())
    return {p for p in palabras if len(p) > 2 and p not in _STOP_WORDS}


def _similitud(a: str, b: str) -> float:
    """Jaccard sobre tokens. Suficiente para proponer la publicacion propia."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return round(len(ta & tb) / len(ta | tb), 3)


def _competidores_raw(alias: str) -> dict:
    data = _load(alias, 'competidores.json', {'competidores': {}})
    if not isinstance(data, dict):
        data = {'competidores': {}}
    data.setdefault('competidores', {})
    return data


def sugerir_item_propio(comp_title: str, items_propios: list[dict],
                        minimo: float = 0.18) -> tuple[str | None, float, list[dict]]:
    """Propone a que publicacion propia asociar un competidor capturado (2.3).

    Devuelve (item_id_sugerido, score, lista_corta) — si acierta, un toque;
    si no, la lista corta para elegir.
    """
    puntuados = []
    for it in items_propios or []:
        iid = it.get('id') or it.get('item_id')
        if not iid:
            continue
        score = _similitud(comp_title, it.get('titulo') or it.get('title') or '')
        puntuados.append({'item_id': iid,
                          'titulo': (it.get('titulo') or it.get('title') or '')[:80],
                          'score': score})
    puntuados.sort(key=lambda x: x['score'], reverse=True)
    corta = puntuados[:5]
    if corta and corta[0]['score'] >= minimo:
        return corta[0]['item_id'], corta[0]['score'], corta
    return None, (corta[0]['score'] if corta else 0.0), corta


def _estado_stock(comp: dict) -> str:
    """Estado de stock inferido del competidor (2.4). Nunca es el numero exacto."""
    status = (comp.get('status') or '').lower()
    if status in ('paused', 'closed', 'inactive'):
        return STOCK_SIN
    cantidad = comp.get('available_quantity')
    if cantidad is not None:
        try:
            cantidad = int(cantidad)
            if cantidad <= 0:
                return STOCK_SIN
            # ML difumina en cantidades altas; suele ser exacto cuando queda poco
            if cantidad <= 3:
                return STOCK_POCO
        except (TypeError, ValueError):
            pass
    senal = (comp.get('senal_stock') or '').lower()
    if 'ultima disponible' in senal or 'última disponible' in senal:
        return STOCK_POCO
    if re.search(r'ultim[ao]s?\s+\d+\s+', senal):
        return STOCK_POCO
    return STOCK_NORMAL


def puntuar_candidato(propio: dict, comp: dict, banda_precio: tuple[float, float] | None = None) -> float:
    """Puntaje 0-1 de que el candidato sea el MISMO producto (2.2.3).

    mismo producto de catalogo +0.5 | atributos clave +0.3 |
    dentro de banda de precio +0.1 | keywords +0.1
    """
    score = 0.0
    cat_propio = propio.get('catalog_product_id')
    if cat_propio and comp.get('catalog_product_id') == cat_propio:
        score += 0.5

    attrs_propio = _attrs_dict(propio.get('attributes'))
    attrs_comp   = _attrs_dict(comp.get('attributes'))
    claves = ('BRAND', 'MODEL', 'POWER', 'ITEM_TYPE', 'CAPACITY', 'PACKAGE_LENGTH',
              'VOLUME_CAPACITY', 'UNITS_PER_PACKAGE', 'SIZE')
    comunes = [k for k in claves if k in attrs_propio and k in attrs_comp]
    if comunes:
        iguales = sum(1 for k in comunes
                      if str(attrs_propio[k]).strip().lower() == str(attrs_comp[k]).strip().lower())
        score += 0.3 * (iguales / len(comunes))

    precio_comp = _num(comp.get('price'))
    if banda_precio and precio_comp:
        if banda_precio[0] <= precio_comp <= banda_precio[1]:
            score += 0.1
    elif precio_comp:
        precio_propio = _num(propio.get('price') or propio.get('precio'))
        if precio_propio and 0.6 * precio_propio <= precio_comp <= 1.4 * precio_propio:
            score += 0.1

    sim = _similitud(propio.get('title') or propio.get('titulo') or '',
                     comp.get('title') or '')
    score += 0.1 * min(sim / 0.4, 1.0)

    return round(min(score, 1.0), 3)


def _attrs_dict(attributes) -> dict:
    out = {}
    for a in attributes or []:
        if isinstance(a, dict):
            aid = a.get('id')
            val = a.get('value_name') or a.get('value')
            if aid and val:
                out[aid] = val
    return out


def guardar_competidor(alias: str, comp: dict, item_propio: str | None = None,
                       clase: str = CLASE_CANDIDATO, puntaje: float | None = None,
                       origen: str = 'bookmarklet') -> dict:
    """Persiste un competidor asociado a UNA publicacion propia (correcciones 1 y 2).

    La clave es (item_propio, comp_id): el mismo competidor puede competir
    contra dos publicaciones distintas con clases distintas.
    """
    comp_id = str(comp.get('id') or '').strip().upper()
    if not comp_id:
        return {}

    data = _competidores_raw(alias)
    clave = f'{item_propio or "_sin_asociar"}::{comp_id}'
    previo = data['competidores'].get(clave, {})

    registro = {
        'clave':         clave,
        'id':            comp_id,
        'item_propio':   item_propio,
        'title':         (comp.get('title') or '')[:200],
        'price':         _num(comp.get('price')) or None,
        'thumbnail':     comp.get('thumbnail') or '',
        'permalink':     comp.get('permalink') or '',
        'seller':        comp.get('seller') or '-',
        'seller_id':     comp.get('seller_id'),
        'catalog_product_id': comp.get('catalog_product_id'),
        'available_quantity': comp.get('available_quantity'),
        'status':        comp.get('status'),
        'senal_stock':   comp.get('senal_stock') or '',
        'attributes':    comp.get('attributes') or [],
        'free_ship':     bool(comp.get('free_ship')),
        'sold_quantity': comp.get('sold_quantity'),
        'clase':         clase,
        'puntaje':       puntaje if puntaje is not None else previo.get('puntaje'),
        'origen':        origen,
        'capturado_ts':  previo.get('capturado_ts') or _ahora(),
        'actualizado_ts': _ahora(),
        'confirmado_por': previo.get('confirmado_por'),
        'historial_precio': previo.get('historial_precio') or [],
    }
    registro['estado_stock'] = _estado_stock(registro)

    # Historial de precio del competidor: alimenta la deteccion de "liquidando"
    precio = registro['price']
    if precio:
        hist = registro['historial_precio']
        hoy = _hoy()
        hist = [h for h in hist if h.get('fecha') != hoy]
        hist.append({'fecha': hoy, 'precio': precio})
        registro['historial_precio'] = hist[-90:]
        if _liquidando(registro['historial_precio']):
            registro['estado_stock'] = STOCK_LIQUIDANDO

    data['competidores'][clave] = registro
    _save(alias, 'competidores.json', data)
    return registro


def _liquidando(historial: list[dict]) -> bool:
    """Bajas fuertes repetidas = liquidando (2.4). No perseguir."""
    precios = [_num(h.get('precio')) for h in (historial or [])[-6:] if h.get('precio')]
    if len(precios) < 3:
        return False
    bajas = sum(1 for i in range(1, len(precios))
                if precios[i] < precios[i - 1] * 0.97)
    return bajas >= 2 and precios[-1] < precios[0] * 0.90


def listar_competidores(alias: str, item_propio: str | None = None,
                        clase: str | None = None) -> list[dict]:
    comps = list(_competidores_raw(alias)['competidores'].values())
    if item_propio:
        comps = [c for c in comps if c.get('item_propio') == item_propio]
    if clase:
        comps = [c for c in comps if c.get('clase') == clase]
    comps.sort(key=lambda c: (_num(c.get('puntaje')), c.get('actualizado_ts', '')), reverse=True)
    return comps


def clasificar_competidor(alias: str, clave: str, clase: str,
                          por: str = 'usuario') -> dict | None:
    """El usuario confirma/rechaza con un toque (2.2.5)."""
    if clase not in (CLASE_DIRECTO, CLASE_SUSTITUTO, CLASE_RUIDO, CLASE_CANDIDATO):
        return None
    data = _competidores_raw(alias)
    reg = data['competidores'].get(clave)
    if not reg:
        return None
    reg['clase'] = clase
    reg['confirmado_por'] = por
    reg['confirmado_ts'] = _ahora()
    _save(alias, 'competidores.json', data)
    return reg


def asociar_competidor(alias: str, clave: str, item_propio: str) -> dict | None:
    """Mueve un competidor capturado sin asociar a una publicacion propia."""
    data = _competidores_raw(alias)
    reg = data['competidores'].pop(clave, None)
    if not reg:
        return None
    reg['item_propio'] = item_propio
    reg['clave'] = f'{item_propio}::{reg["id"]}'
    reg['actualizado_ts'] = _ahora()
    data['competidores'][reg['clave']] = reg
    _save(alias, 'competidores.json', data)
    return reg


def eliminar_competidor(alias: str, clave: str) -> bool:
    data = _competidores_raw(alias)
    if data['competidores'].pop(clave, None) is None:
        return False
    _save(alias, 'competidores.json', data)
    return True


def competidores_para_precio(alias: str, item_propio: str) -> list[dict]:
    """UNICA fuente valida para mover precio (2.2.4 y 2.2.6).

    Solo `directo` confirmados, o automaticos con puntaje >= 0.85 Y mismo
    producto de catalogo. Nunca mueve precio contra un candidato sin confirmar.
    """
    validos = []
    for c in listar_competidores(alias, item_propio=item_propio):
        if c.get('clase') != CLASE_DIRECTO:
            continue
        if c.get('confirmado_por'):
            validos.append(c)
            continue
        if (_num(c.get('puntaje')) >= PUNTAJE_MIN_AUTO_DIRECTO
                and c.get('catalog_product_id')):
            validos.append(c)
    return validos


def candidatos_pendientes(alias: str, item_propio: str | None = None) -> list[dict]:
    """Candidatos con puntaje suficiente esperando confirmacion (bandeja, 2.5)."""
    out = []
    for c in listar_competidores(alias, item_propio=item_propio):
        if c.get('clase') != CLASE_CANDIDATO or c.get('confirmado_por'):
            continue
        if c.get('puntaje') is None or _num(c.get('puntaje')) >= PUNTAJE_MIN_CANDIDATO:
            out.append(c)
    return out


def resumen_competidores(alias: str) -> dict:
    comps = listar_competidores(alias)
    por_item: dict[str, dict] = {}
    for c in comps:
        item = c.get('item_propio') or '_sin_asociar'
        e = por_item.setdefault(item, {'directos': 0, 'sustitutos': 0,
                                       'candidatos': 0, 'ruido': 0})
        clase = c.get('clase')
        if clase == CLASE_DIRECTO:
            e['directos'] += 1
        elif clase == CLASE_SUSTITUTO:
            e['sustitutos'] += 1
        elif clase == CLASE_RUIDO:
            e['ruido'] += 1
        else:
            e['candidatos'] += 1
    return {'total': len(comps), 'por_item': por_item,
            'pendientes': len(candidatos_pendientes(alias))}


# ══════════════════════════════════════════════════════════════════════════════
# Resumen para la UI / Telegram (bandeja unica, bloque 7)
# ══════════════════════════════════════════════════════════════════════════════

def resumen(alias: str) -> dict:
    """Estado de Cerebro para una cuenta. Barato: solo lee JSON."""
    acciones = _acciones_raw(alias)['acciones']
    por_estado: dict[str, int] = {}
    for a in acciones:
        por_estado[a.get('estado', '?')] = por_estado.get(a.get('estado', '?'), 0) + 1

    aps = listar_aprendizajes(alias)
    snaps = _snapshots_raw(alias)['items']
    dias_serie = max((len(v.get('serie', [])) for v in snaps.values()), default=0)

    return {
        'alias':             alias,
        'acciones_total':    len(acciones),
        'acciones_por_estado': por_estado,
        'pendientes':        por_estado.get(ESTADO_PENDIENTE, 0),
        'aprendizajes':      len(aps),
        'aprendizajes_confiables': sum(1 for a in aps.values()
                                       if a.get('confianza') in ('media', 'alta')),
        'items_con_serie':   len(snaps),
        'dias_serie_max':    dias_serie,
        'competidores':      resumen_competidores(alias),
    }
