"""Comisión por publicación según sus cuotas sin interés."""
from core.fees import comision_item, cuotas_de_item
from modules.stock_rentabilidad import _calcular_margen


def test_cuotas_por_tag():
    assert cuotas_de_item('gold_special', ['3x_campaign']) == 1     # Clásica no tiene cuotas
    assert cuotas_de_item('gold_pro', []) == 6                      # Premium sin tag = 6
    assert cuotas_de_item('gold_pro', ['mshops_3x_campaign', '3x_campaign']) == 3
    assert cuotas_de_item('gold_pro', ['12x_campaign']) == 12


def test_comision_item_pide_con_el_tag_de_cuotas():
    pedidos = []

    class C:
        def _get(self, path, params=None):
            pedidos.append(params)
            return [{'sale_fee_amount': 17112.0}]
    assert comision_item(C(), 68724, 'gold_pro', 'MLA414133', ['mshops_3x_campaign', '3x_campaign']) == 0.249
    assert pedidos[0]['tags'] == '3x_campaign' and pedidos[0]['category_id'] == 'MLA414133'


def test_margen_usa_primero_la_comision_actual_del_item():
    m = _calcular_margen(68724, 12000, 'gold_pro', real_fee_rate=0.20, fees={'gold_pro': 0.397},
                         item_fee_rate=0.249)
    assert (m['fee_rate'], m['fee_source']) == (0.249, 'ml_item')
    # si las ventas reales pagaron más que la tarifa de hoy, manda lo real
    m = _calcular_margen(48000, 13000, 'gold_special', real_fee_rate=0.2809, item_fee_rate=0.16)
    assert (m['fee_rate'], m['fee_source']) == (0.2809, 'real')
    m = _calcular_margen(68724, 12000, 'gold_pro', real_fee_rate=0.30, fees={'gold_pro': 0.397})
    assert m['fee_source'] == 'real'
