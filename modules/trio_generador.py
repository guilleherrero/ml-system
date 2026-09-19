"""
Tasas reales de ML por escalon de cuotas — Calculadora de Estrategia de Precios.

El generador de titulos del trio (que vivia en este archivo) se eliminó el
2026-09-19 a pedido de Guille: "Generador de trío nunca funciono... prefiero
que lo quites de ahi" — ver docs/CEREBRO.md seccion 12. Queda solo
`tasas_reales_por_escalon`, que sigue en uso por `/api/pricing/contexto`
para mostrar comisión/cuotas reales por producto (no el ejemplo genérico).
"""

# ── Escalon de cuotas -> (listing_type_id, tags) — ver docs/CEREBRO.md seccion 12 ──
# Confirmado contra documentacion oficial de ML (2026-09-18): el escalon SI se
# puede fijar por API, via el campo `tags` combinado con `listing_type_id`.
CUOTAS_A_TAGS = {
    0:  ("gold_special", []),                    # sin cuotas propias (Batalla)
    4:  ("gold_special", ["pcj-co-funded"]),      # interes bajo, comprador elige 3-12
    3:  ("gold_pro", ["3x_campaign"]),
    6:  ("gold_pro", []),                          # default de gold_pro, sin tag
    9:  ("gold_pro", ["9x_campaign"]),
    12: ("gold_pro", ["12x_campaign"]),
}


def tasas_reales_por_escalon(client, domain_id: str, precio_referencia: float) -> dict:
    """Comision pura y costo de cuotas real por escalon, consultando
    /sites/MLA/listing_prices EN VIVO para el domain_id real del producto.

    Verificado en vivo 2026-09-19 (cuenta NOVARA, dominio
    HAIR_CLIPPERS_ELECTRIC_SHAVERS_AND_HAIR_TRIMMERS): `percentage_fee` NO
    es la comision pura — ya trae sumado el `financing_add_on_fee` adentro
    (percentage_fee = meli_percentage_fee + financing_add_on_fee, confirmado
    en los 6 escalones). La comision pura es `meli_percentage_fee`, y se
    mantuvo CONSTANTE (16%) en los 6 escalones de esta prueba — el costo de
    cuotas varia por escalon Y por dominio (acá: 0/5/8.9/13.4/17.8/21.6%
    para sin-cuotas/interes-bajo/3/6/9/12 cuotas — el interes bajo dio 5%,
    no el 4% que traía un ejemplo generico de la documentacion. Nunca
    asumir un numero fijo sin consultarlo para el dominio real.

    Devuelve {escalon: {"comision": float|None, "costo_cuotas": float|None}}
    — algun escalon puede faltar si la categoria no lo tiene habilitado.
    """
    import requests as _req
    token = client.account.access_token
    headers = {'Authorization': f'Bearer {token}'}
    precio = max(precio_referencia, 100000)  # arriba del umbral, para que el cargo fijo no distorsione el %
    tasas = {}
    for escalon, (listing_type, tags) in CUOTAS_A_TAGS.items():
        params = {'price': precio, 'listing_type_id': listing_type, 'domain_id': domain_id}
        if tags:
            params['tags'] = tags[0]
        try:
            r = _req.get('https://api.mercadolibre.com/sites/MLA/listing_prices',
                         headers=headers, params=params, timeout=8)
            if r.ok:
                data = r.json()
                d = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
                sfd = d.get('sale_fee_details', {})
                if sfd:
                    tasas[escalon] = {
                        'comision': sfd.get('meli_percentage_fee'),
                        'costo_cuotas': sfd.get('financing_add_on_fee'),
                    }
        except Exception:
            pass
    return tasas
