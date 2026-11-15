"""
Historial de ventas — sección aparte (pedido de Guille, 2026-11-15).

Problema: "cada día vendo menos y no entiendo por qué". Ventas por producto solo
mira 30 días y corta en 500 órdenes, así que no hay forma de ver la tendencia.

Este módulo:
  1. Trae las órdenes pagas de la cuenta mes por mes (`GET /orders/search`) y las
     guarda agregadas por día y por publicación. Los meses cerrados quedan
     guardados; el mes en curso y el anterior se vuelven a pedir en cada
     actualización (ahí pueden aparecer cancelaciones o pagos tardíos).
  2. Analiza la serie: evolución mensual y semanal, qué publicaciones caen o
     suben (últimos 30 días contra los 30 anteriores), días sin vender y una
     proyección simple a 30 días.

Solo lee: no toca nada en MercadoLibre.
"""
import time
from datetime import date, datetime, timedelta, timezone

AR = timezone(timedelta(hours=-3))

from core.db_storage import db_load, db_save

MESES_DEFAULT = 12
LIMITE_ML = 50          # máximo que acepta /orders/search por página
TOPE_OFFSET = 9950      # ML no pagina más allá de ~10.000 resultados por consulta
DIAS_SEMANA = ['Lun', 'Mar', 'Mié', 'Jue', 'Vie', 'Sáb', 'Dom']


def _safe(alias: str) -> str:
    return (alias or '').replace(' ', '_').replace('/', '-')


def _path(alias: str) -> str:
    return f'data/historial_ventas_{_safe(alias)}.json'


def _seller_id(client):
    if not client.account.user_id:
        client.account.user_id = (client.get_me() or {}).get('id')
    return client.account.user_id


def cargar(alias: str) -> dict:
    return db_load(_path(alias)) or {'alias': alias, 'meses': {}, 'titulos': {}}


def meses_a_sincronizar(alias: str, meses: int = MESES_DEFAULT, hoy: date | None = None) -> list[str]:
    """Meses (AAAA-MM) que faltan pedir. El actual y el anterior se piden siempre."""
    hoy = hoy or datetime.now(AR).date()
    guardado = cargar(alias).get('meses', {})
    lista = []
    y, m = hoy.year, hoy.month
    for i in range(meses):
        clave = f'{y:04d}-{m:02d}'
        if i < 2 or clave not in guardado:
            lista.append(clave)
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(lista))


def _ventanas_del_mes(mes: str):
    """Parte el mes en tramos de 7 días para no chocar con el tope de paginación."""
    y, m = map(int, mes.split('-'))
    ini = date(y, m, 1)
    fin = date(y + (m == 12), (m % 12) + 1, 1)
    d = ini
    while d < fin:
        h = min(d + timedelta(days=7), fin)
        yield d, h
        d = h


def _traer_ordenes(client, seller_id, desde: date, hasta: date) -> list[dict]:
    ordenes, offset = [], 0
    while True:
        data = client._get('/orders/search', params={
            'seller': seller_id,
            'order.status': 'paid',
            'order.date_created.from': f'{desde.isoformat()}T00:00:00.000-03:00',
            'order.date_created.to': f'{(hasta - timedelta(days=1)).isoformat()}T23:59:59.999-03:00',
            'sort': 'date_asc',
            'limit': LIMITE_ML,
            'offset': offset,
        })
        lote = data.get('results', []) or []
        ordenes.extend(lote)
        total = (data.get('paging') or {}).get('total', 0)
        offset += len(lote)
        if not lote or offset >= total or offset >= TOPE_OFFSET:
            break
        time.sleep(0.15)
    return ordenes


def agregar_ordenes(ordenes: list[dict]) -> tuple[dict, dict]:
    """Devuelve ({dia: {item_id: [unidades, ingresos, ordenes]}}, {item_id: titulo})."""
    dias, titulos = {}, {}
    for o in ordenes:
        dia = (o.get('date_created') or '')[:10]
        if not dia:
            continue
        vistos = set()
        for oi in o.get('order_items', []) or []:
            it = oi.get('item') or {}
            iid = it.get('id')
            if not iid:
                continue
            qty = int(oi.get('quantity') or 1)
            precio = float(oi.get('unit_price') or 0)
            fila = dias.setdefault(dia, {}).setdefault(iid, [0, 0.0, 0])
            fila[0] += qty
            fila[1] = round(fila[1] + precio * qty, 2)
            if iid not in vistos:
                fila[2] += 1
                vistos.add(iid)
            if it.get('title'):
                titulos[iid] = it['title'][:90]
    return dias, titulos


def sincronizar_mes(client, alias: str, mes: str) -> dict:
    """Pide a ML las órdenes pagas de un mes y lo guarda. Devuelve un resumen del mes."""
    seller_id = _seller_id(client)
    ordenes = []
    for desde, hasta in _ventanas_del_mes(mes):
        ordenes.extend(_traer_ordenes(client, seller_id, desde, hasta))
    dias, titulos = agregar_ordenes(ordenes)
    data = cargar(alias)
    data.setdefault('meses', {})[mes] = {
        'sync_at': datetime.now(AR).isoformat(timespec='seconds'),
        'dias': dias,
    }
    data.setdefault('titulos', {}).update(titulos)
    data['alias'] = alias
    data['actualizado'] = datetime.now(AR).isoformat(timespec='seconds')
    db_save(_path(alias), data)
    unidades = sum(v[0] for d in dias.values() for v in d.values())
    return {'mes': mes, 'ordenes': len(ordenes), 'unidades': unidades}


# ── Análisis ─────────────────────────────────────────────────────────────────

def _serie_diaria(data: dict) -> dict:
    """{dia: {item_id: [u, ing, ord]}} con todos los meses juntos."""
    out = {}
    for m in (data.get('meses') or {}).values():
        out.update(m.get('dias') or {})
    return out


def _sumar(serie: dict, desde: date, hasta: date, item: str | None = None) -> tuple[int, float]:
    """Unidades e ingresos entre desde (incl.) y hasta (excl.)."""
    u, ing = 0, 0.0
    d = desde
    while d < hasta:
        por_item = serie.get(d.isoformat()) or {}
        if item is None:
            for v in por_item.values():
                u += v[0]
                ing += v[1]
        elif item in por_item:
            u += por_item[item][0]
            ing += por_item[item][1]
        d += timedelta(days=1)
    return u, round(ing, 2)


def _var(actual: float, anterior: float):
    if anterior <= 0:
        return None if actual <= 0 else 100.0
    return round((actual - anterior) / anterior * 100, 1)


def _estado(u30: int, u_prev: int, var_pct) -> str:
    if u30 == 0 and u_prev == 0:
        return 'sin_ventas'
    if u30 == 0:
        return 'se_corto'
    if var_pct is None:
        return 'nuevo'
    if var_pct <= -40:
        return 'cae_fuerte'
    if var_pct <= -15:
        return 'cae'
    if var_pct >= 25:
        return 'sube'
    return 'estable'


def proyeccion_30d(u_ult14: int, u_prev14: int) -> int:
    """Promedio de las últimas 4 semanas ajustado (suave) por la tendencia reciente."""
    base = (u_ult14 + u_prev14) / 28
    if u_prev14 > 0:
        factor = max(0.7, min(1.3, u_ult14 / u_prev14)) ** 0.5
    else:
        factor = 1.0
    return round(base * 30 * factor)


def analizar(alias: str, hoy: date | None = None) -> dict:
    data = cargar(alias)
    serie = _serie_diaria(data)
    titulos = data.get('titulos') or {}
    hoy = hoy or datetime.now(AR).date()
    manana = hoy + timedelta(days=1)
    if not serie:
        return {'vacio': True, 'actualizado': data.get('actualizado')}

    # Totales de la cuenta
    u30, ing30 = _sumar(serie, manana - timedelta(days=30), manana)
    u_prev, ing_prev = _sumar(serie, manana - timedelta(days=60), manana - timedelta(days=30))
    u7, ing7 = _sumar(serie, manana - timedelta(days=7), manana)
    u7_prev, _ = _sumar(serie, manana - timedelta(days=14), manana - timedelta(days=7))
    u_anio, ing_anio = _sumar(serie, manana - timedelta(days=365), manana - timedelta(days=335))

    # Serie mensual
    mensual = []
    for mes in sorted((data.get('meses') or {}).keys()):
        dias = data['meses'][mes].get('dias') or {}
        mensual.append({
            'mes': mes,
            'unidades': sum(v[0] for d in dias.values() for v in d.values()),
            'ingresos': round(sum(v[1] for d in dias.values() for v in d.values()), 2),
            'ordenes': sum(v[2] for d in dias.values() for v in d.values()),
        })

    # Serie semanal (últimas 26 semanas, terminando hoy)
    semanal = []
    for i in range(25, -1, -1):
        fin = manana - timedelta(days=7 * i)
        u, ing = _sumar(serie, fin - timedelta(days=7), fin)
        semanal.append({'semana': (fin - timedelta(days=7)).isoformat(), 'unidades': u, 'ingresos': ing})

    # Patrón por día de la semana (últimos 90 días)
    por_dow = [0] * 7
    d = manana - timedelta(days=90)
    while d < manana:
        por_dow[d.weekday()] += sum(v[0] for v in (serie.get(d.isoformat()) or {}).values())
        d += timedelta(days=1)

    # Por publicación
    ultima_venta = {}
    for dia in sorted(serie):
        for iid in serie[dia]:
            ultima_venta[iid] = dia
    items = []
    for iid in ultima_venta:
        i30, g30 = _sumar(serie, manana - timedelta(days=30), manana, iid)
        ip, gp = _sumar(serie, manana - timedelta(days=60), manana - timedelta(days=30), iid)
        i14, _ = _sumar(serie, manana - timedelta(days=14), manana, iid)
        ip14, _ = _sumar(serie, manana - timedelta(days=28), manana - timedelta(days=14), iid)
        i365, g365 = _sumar(serie, manana - timedelta(days=365), manana, iid)
        semanas = []
        for k in range(11, -1, -1):
            fin = manana - timedelta(days=7 * k)
            semanas.append(_sumar(serie, fin - timedelta(days=7), fin, iid)[0])
        var = _var(i30, ip)
        dias_sin = max(0, (hoy - date.fromisoformat(ultima_venta[iid])).days)
        items.append({
            'item_id': iid,
            'titulo': titulos.get(iid, iid),
            'unidades_30d': i30, 'unidades_30d_prev': ip, 'var_pct': var,
            'ingresos_30d': g30, 'ingresos_30d_prev': gp,
            'precio_prom_30d': round(g30 / i30, 2) if i30 else None,
            'precio_prom_30d_prev': round(gp / ip, 2) if ip else None,
            'unidades_12m': i365, 'ingresos_12m': g365,
            'semanas': semanas,
            'ultima_venta': ultima_venta[iid], 'dias_sin_vender': dias_sin,
            'proyeccion_30d': proyeccion_30d(i14, ip14),
            'estado': _estado(i30, ip, var),
            'unidades_perdidas': max(0, ip - i30),
        })
    items.sort(key=lambda x: (-x['ingresos_30d'], -x['ingresos_12m']))

    caidas = sorted([x for x in items if x['unidades_perdidas'] > 0],
                    key=lambda x: -(x['ingresos_30d_prev'] - x['ingresos_30d']))[:10]
    subas = sorted([x for x in items if x['unidades_30d'] > x['unidades_30d_prev']],
                   key=lambda x: -(x['ingresos_30d'] - x['ingresos_30d_prev']))[:10]

    perdida_ing = sum(max(0, x['ingresos_30d_prev'] - x['ingresos_30d']) for x in items)
    explicado = sum(max(0, x['ingresos_30d_prev'] - x['ingresos_30d']) for x in caidas[:3])

    return {
        'vacio': False,
        'actualizado': data.get('actualizado'),
        'meses_guardados': len(mensual),
        'resumen': {
            'unidades_30d': u30, 'ingresos_30d': ing30,
            'unidades_30d_prev': u_prev, 'ingresos_30d_prev': ing_prev,
            'var_unidades_pct': _var(u30, u_prev), 'var_ingresos_pct': _var(ing30, ing_prev),
            'unidades_7d': u7, 'unidades_7d_prev': u7_prev, 'var_7d_pct': _var(u7, u7_prev),
            'unidades_anio_pasado': u_anio, 'ingresos_anio_pasado': ing_anio,
            'var_anual_pct': _var(u30, u_anio) if u_anio else None,
            'proyeccion_30d': sum(x['proyeccion_30d'] for x in items),
            'ingresos_perdidos_30d': round(perdida_ing, 2),
            'pct_caida_top3': round(explicado / perdida_ing * 100) if perdida_ing else None,
        },
        'mensual': mensual,
        'semanal': semanal,
        'por_dia_semana': [{'dia': DIAS_SEMANA[i], 'unidades': por_dow[i]} for i in range(7)],
        'caidas': caidas,
        'subas': subas,
        'items': items,
    }
