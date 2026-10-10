"""
Analista de Meli Ads — qué hacer con la publicidad, en palabras simples.

La pantalla anterior mostraba números de campañas sin decir qué hacer con
ellos. Este módulo cruza lo que gasta cada producto en publicidad con SU
margen real (comisión con cuotas + envío + costo, del análisis diario) y
devuelve acciones ordenadas por plata en juego.

La cuenta que decide todo: un producto con margen 30% puede gastar en
publicidad hasta 30% de lo que vende por publicidad (ACoS) sin perder plata.
Arriba de eso, cada venta por publicidad le cuesta al vendedor.

Las reglas son fijas y explicables (sin IA): el vendedor tiene que poder
entender por qué se le dice cada cosa.
"""

from __future__ import annotations

from datetime import date, timedelta

from modules import meli_ads_engine as eng

METRICAS = ('clicks,prints,cost,acos,units_quantity,total_amount,'
            'direct_amount,indirect_amount,direct_units_quantity,indirect_units_quantity')

GASTO_SIN_VENTAS = 5000      # ARS en 30 días sin ninguna venta por publicidad
DIAS_STOCK_MIN = 7           # con menos, la publicidad trae ventas que no se van a poder cumplir
MARGEN_PARA_SUMAR = 0.25     # producto rentable que conviene sumar a publicidad
PRESUPUESTO_AGOTADO = 0.9    # gasto de hoy / presupuesto diario


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def leer(token: str, dias: int = 30) -> dict:
    """Campañas y anuncios (con métricas) de Product Ads v2."""
    hoy = date.today()
    desde, hasta = (hoy - timedelta(days=dias)).isoformat(), hoy.isoformat()
    r = eng._ads_get('/advertising/advertisers', token, params={'product_id': 'PADS'}, api_version='1')
    if r['status'] in (401, 403):
        return {'error': 'Tu cuenta no le dio permiso de Publicidad al sistema. Reconectala.'}
    advs = ((r['data'] or {}).get('advertisers') or []) if r['ok'] else []
    if not advs:
        return {'error': 'Esta cuenta no tiene Product Ads.' if r['ok']
                else f'Mercado Libre no respondió (HTTP {r["status"]}).'}
    a = advs[0]
    base = f"/marketplace/advertising/{a.get('site_id') or 'MLA'}/advertisers/{a.get('advertiser_id')}/product_ads"
    q = {'date_from': desde, 'date_to': hasta, 'metrics': METRICAS}
    camps, st = eng._mkt_paginado(f'{base}/campaigns/search', token, q)
    if st != 200 and not camps:
        return {'error': f'No se pudieron leer las campañas (HTTP {st}).'}
    ads, _ = eng._mkt_paginado(f'{base}/ads/search', token, q)
    hoy_c, _ = eng._mkt_paginado(f'{base}/campaigns/search', token,
                                 {'date_from': hasta, 'date_to': hasta, 'metrics': 'cost'})
    gasto_hoy = {c.get('id'): _f((c.get('metrics') or {}).get('cost')) for c in hoy_c}
    return {'campanias': camps, 'anuncios': ads, 'gasto_hoy': gasto_hoy,
            'desde': desde, 'hasta': hasta, 'advertiser_id': a.get('advertiser_id')}


def _ventas(m: dict) -> tuple[float, int]:
    monto = _f(m.get('total_amount')) or _f(m.get('direct_amount')) + _f(m.get('indirect_amount'))
    unid = int(_f(m.get('units_quantity')) or _f(m.get('direct_units_quantity')) + _f(m.get('indirect_units_quantity')))
    return monto, unid


def analizar(datos: dict, stock: dict) -> dict:
    """Arma resumen, acciones, campañas y productos.

    stock: {item_id: fila del análisis diario} (precio, margen_pct, stock, dias_stock,
    ventas_30d, visitas_30d, conversion_pct, titulo).
    """
    camps = {c.get('id'): c for c in datos.get('campanias', [])}
    productos, acciones = [], []
    con_ads = set()

    for ad in datos.get('anuncios', []):
        iid = str(ad.get('item_id') or '')
        con_ads.add(iid)
        m = ad.get('metrics') or {}
        gasto, clics, vistas = _f(m.get('cost')), int(_f(m.get('clicks'))), int(_f(m.get('prints')))
        venta, unid = _ventas(m)
        acos = gasto / venta if venta else None
        st = stock.get(iid) or {}
        margen = st.get('margen_pct')            # ya descuenta comisión con cuotas, envío y costo
        camp = camps.get(ad.get('campaign_id')) or {}
        # Lo que queda después de pagar la publicidad (sobre las ventas que trajo)
        resultado = (venta * margen - gasto) if margen is not None else None
        p = {
            'item_id': iid, 'titulo': ad.get('title') or st.get('titulo') or iid,
            'campania_id': ad.get('campaign_id'), 'campania': camp.get('name', ''),
            'estado': ad.get('status', ''), 'precio': _f(ad.get('price')),
            'gasto': round(gasto), 'ventas_ads': round(venta), 'unidades_ads': unid,
            'clics': clics, 'vistas': vistas, 'acos': acos, 'margen': margen,
            'resultado': round(resultado) if resultado is not None else None,
            'stock': st.get('stock'), 'dias_stock': st.get('dias_stock'),
        }
        p['veredicto'], p['motivo'] = _veredicto(p)
        productos.append(p)
        a = _accion_producto(p)
        if a:
            acciones.append(a)

    campanias = []
    for cid, c in camps.items():
        m = c.get('metrics') or {}
        gasto = _f(m.get('cost'))
        venta, unid = _ventas(m)
        sus = [p for p in productos if p['campania_id'] == cid]
        res = [p['resultado'] for p in sus if p['resultado'] is not None]
        ppto = _f(c.get('daily_budget') or c.get('budget'))
        hoy = datos.get('gasto_hoy', {}).get(cid, 0.0)
        k = {
            'id': cid, 'nombre': c.get('name') or f'Campaña {cid}', 'estado': c.get('status', ''),
            'presupuesto': round(ppto), 'gasto_hoy': round(hoy),
            'objetivo_acos': _f(c.get('acos_target')) / 100 if c.get('acos_target') else None,
            'gasto': round(gasto), 'ventas_ads': round(venta), 'unidades_ads': unid,
            'acos': gasto / venta if venta else None,
            'resultado': round(sum(res)) if res else None,
            'productos': len(sus), 'ganan': sum(1 for p in sus if p['veredicto'] == 'gana'),
            'pierden': sum(1 for p in sus if p['veredicto'] == 'pierde'),
        }
        k['veredicto'] = ('pausada' if k['estado'] != 'active' else
                          'pierde' if (k['resultado'] or 0) < 0 else
                          'gana' if (k['resultado'] or 0) > 0 else 'sin_datos')
        campanias.append(k)
        a = _accion_campania(k)
        if a:
            acciones.append(a)

    # Productos rentables que venden solos y no tienen publicidad
    for iid, st in stock.items():
        if iid in con_ads or (st.get('stock') or 0) <= 0:
            continue
        mg = st.get('margen_pct')
        if mg is not None and mg >= MARGEN_PARA_SUMAR and (st.get('conversion_pct') or 0) >= 1 \
                and (st.get('dias_stock') or 999) >= 30:
            acciones.append({
                'tipo': 'sumar', 'prioridad': 3, 'item_id': iid, 'titulo': st.get('titulo', iid),
                'que': 'Sumalo a publicidad',
                'porque': (f"Tiene {mg * 100:.0f}% de margen y convierte {st.get('conversion_pct'):.1f}% de las visitas, "
                           f"con stock para {int(st.get('dias_stock') or 0)} días. Puede pagar publicidad "
                           f"hasta {mg * 100:.0f}% de ACoS sin perder."),
                'plata': round((st.get('precio') or 0) * mg * 3),
            })

    acciones.sort(key=lambda a: (a['prioridad'], -(a.get('plata') or 0)))
    gasto_t = sum(c['gasto'] for c in campanias)
    venta_t = sum(c['ventas_ads'] for c in campanias)
    res_t = [p['resultado'] for p in productos if p['resultado'] is not None]
    return {
        'resumen': {
            'gasto': gasto_t, 'ventas_ads': venta_t, 'acos': gasto_t / venta_t if venta_t else None,
            'resultado': round(sum(res_t)) if res_t else None,
            'sin_margen': sum(1 for p in productos if p['margen'] is None),
            'campanias_activas': sum(1 for c in campanias if c['estado'] == 'active'),
            'productos': len(productos), 'desde': datos.get('desde'), 'hasta': datos.get('hasta'),
        },
        'acciones': acciones,
        'campanias': sorted(campanias, key=lambda c: -c['gasto']),
        'productos': sorted(productos, key=lambda p: -p['gasto']),
    }


def _veredicto(p: dict) -> tuple[str, str]:
    if p['gasto'] == 0:
        return 'sin_gasto', 'No gastó en el período'
    if p['margen'] is None:
        return 'sin_datos', 'Falta el costo del producto para saber si gana'
    if p['ventas_ads'] == 0:
        return 'pierde', f"Gastó ${p['gasto']:,.0f} y no vendió nada por publicidad".replace(',', '.')
    if p['acos'] > p['margen']:
        return 'pierde', (f"Gasta {p['acos'] * 100:.0f}% de lo que vende en publicidad y su margen es "
                          f"{p['margen'] * 100:.0f}%: cada venta por publicidad pierde plata")
    if p['acos'] > p['margen'] * 0.7:
        return 'justo', (f"Gana poco: gasta {p['acos'] * 100:.0f}% de lo que vende y el margen es "
                         f"{p['margen'] * 100:.0f}%")
    return 'gana', (f"Gasta {p['acos'] * 100:.0f}% de lo que vende y el margen es "
                    f"{p['margen'] * 100:.0f}%: la publicidad deja ganancia")


def _plata(n: float) -> str:
    return f"${n:,.0f}".replace(',', '.')


def _accion_producto(p: dict) -> dict | None:
    base = {'item_id': p['item_id'], 'titulo': p['titulo'], 'campania': p['campania']}
    if p['gasto'] > 0 and p['stock'] is not None and (p['stock'] <= 0 or (p['dias_stock'] or 999) < DIAS_STOCK_MIN):
        return {**base, 'tipo': 'pausar_stock', 'prioridad': 1, 'plata': p['gasto'],
                'que': 'Pausá el anuncio: se está quedando sin stock',
                'porque': (f"Le queda{'' if p['stock'] == 1 else 'n'} {p['stock']} unidad{'es' if p['stock'] != 1 else ''}"
                           f"{f' (≈{int(p['dias_stock'])} días)' if p['dias_stock'] else ''}. "
                           "Seguir pagando publicidad trae ventas que no vas a poder cumplir.")}
    if p['ventas_ads'] == 0 and p['gasto'] >= GASTO_SIN_VENTAS:
        return {**base, 'tipo': 'sacar', 'prioridad': 1, 'plata': p['gasto'],
                'que': 'Sacalo de la publicidad (o mejorá la publicación primero)',
                'porque': (f"Gastó {_plata(p['gasto'])} en 30 días, tuvo {p['clics']} clics y ninguna venta. "
                           "La gente entra y no compra: el problema está en la publicación o el precio, no en la publicidad.")}
    if p['veredicto'] == 'pierde' and p['ventas_ads'] > 0:
        perdida = -(p['resultado'] or 0)
        return {**base, 'tipo': 'perdida', 'prioridad': 1 if perdida > 10000 else 2, 'plata': perdida,
                'que': f"Pierde plata en «{p['campania']}»: bajá su ACoS objetivo o sacalo",
                'porque': (f"Gastó {_plata(p['gasto'])} para vender {_plata(p['ventas_ads'])} "
                           f"(ACoS {p['acos'] * 100:.0f}%). Su margen es {p['margen'] * 100:.0f}%, "
                           f"así que la publicidad le hizo perder ≈{_plata(perdida)}.")}
    if p['veredicto'] == 'gana' and p['unidades_ads'] >= 3 and p['acos'] < p['margen'] * 0.5:
        return {**base, 'tipo': 'potenciar', 'prioridad': 3, 'plata': p['resultado'] or 0,
                'que': 'Funciona muy bien: dale más empuje',
                'porque': (f"Vendió {p['unidades_ads']} unidades por publicidad gastando solo "
                           f"{p['acos'] * 100:.0f}% de lo que vendió (su margen es {p['margen'] * 100:.0f}%). "
                           "Pasalo a una campaña con más presupuesto o subí el ACoS objetivo para que se muestre más.")}
    return None


def _accion_campania(k: dict) -> dict | None:
    base = {'campania': k['nombre'], 'campania_id': k['id']}
    if k['estado'] != 'active':
        return None
    if k['presupuesto'] and k['gasto_hoy'] >= k['presupuesto'] * PRESUPUESTO_AGOTADO and (k['resultado'] or 0) > 0:
        return {**base, 'tipo': 'presupuesto', 'prioridad': 2, 'plata': k['resultado'],
                'que': f"Subí el presupuesto de «{k['nombre']}»",
                'porque': (f"Hoy ya gastó {_plata(k['gasto_hoy'])} de {_plata(k['presupuesto'])} por día y en 30 días "
                           f"dejó {_plata(k['resultado'])} de ganancia después de publicidad. Se queda sin plata "
                           "y deja de mostrarse en las horas que vende.")}
    if (k['resultado'] or 0) < 0:
        return {**base, 'tipo': 'campania_pierde', 'prioridad': 1, 'plata': -k['resultado'],
                'que': f"La campaña «{k['nombre']}» pierde plata",
                'porque': (f"Gastó {_plata(k['gasto'])} y vendió {_plata(k['ventas_ads'])}: después de pagar la "
                           f"publicidad quedan {_plata(k['resultado'])}. {k['pierden']} de sus {k['productos']} "
                           "productos pierden; revisalos abajo.")}
    return None
