from concurrent.futures import ThreadPoolExecutor

from web.calculadora_routes import tramos


def test_tramos_detecta_cortes_reales():
    # tabla ML verificada 2026-11-20: 0 bajo 1000, 1330 / 2740 / 3320, 0 desde 33.000
    def fijo(p):
        return 0 if p < 1000 else 1330 if p < 15000 else 2740 if p < 24000 else 3320 if p < 33000 else 0
    with ThreadPoolExecutor(4) as pool:
        assert tramos(fijo, 60000, 2500, pool) == [
            {'desde': 0, 'valor': 0}, {'desde': 1000, 'valor': 1330}, {'desde': 15000, 'valor': 2740},
            {'desde': 24000, 'valor': 3320}, {'desde': 33000, 'valor': 0}]
