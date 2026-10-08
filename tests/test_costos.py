"""Costos con una sola fuente (tabla contable) y costos.json como espejo.

Lo crítico: ningún costo cargado se pierde al unificar, y la historia
(vigente_desde) se respeta para que Contabilidad valúe cada venta con el
costo de su fecha.
"""

import os
import sys
import tempfile
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ['DATABASE_URL'] = ''

import web.db as webdb  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from modules import costos  # noqa: E402

HOY = date.today()


@pytest.fixture
def entorno(monkeypatch, tmp_path):
    eng = create_engine(f'sqlite:///{tmp_path / "c.db"}', future=True)
    monkeypatch.setattr(webdb, 'engine', eng)
    monkeypatch.setattr(webdb, 'SessionFactory', sessionmaker(bind=eng, autoflush=False, future=True))
    from web import models_contabilidad  # noqa: F401
    webdb.Base.metadata.create_all(eng)
    store = {}
    load = lambda p: store.get(os.path.normpath(p))  # noqa: E731
    save = lambda p, d: store.__setitem__(os.path.normpath(p), d)  # noqa: E731
    monkeypatch.setattr(costos, 'db_load', load)
    monkeypatch.setattr(costos, 'db_save', save)
    import core.db_storage as dbs
    monkeypatch.setattr(dbs, 'db_load', load)
    import core.account_manager as am
    monkeypatch.setattr(am.AccountManager, 'list_accounts', lambda self: [])
    return store


def espejo(store):
    return store.get(os.path.normpath(costos.ESPEJO)) or {}


def test_unificar_trae_lo_que_solo_estaba_en_el_json(entorno):
    entorno[os.path.normpath(costos.ESPEJO)] = {
        'MLA1': {'alias': 'Novara', 'titulo': 'Faja', 'costo': 9000, 'updated': '2026-05-01'}}
    res = costos.unificar_una_vez()
    assert res['cargados'] == 1
    assert costos.vigentes()['MLA1']['costo'] == 9000.0
    assert espejo(entorno)['MLA1']['alias'] == 'Novara'      # el alias se conserva
    assert costos.unificar_una_vez() is None                  # corre una sola vez


def test_guardar_antes_de_migrar_no_borra_costos_viejos(entorno):
    entorno[os.path.normpath(costos.ESPEJO)] = {
        'MLA1': {'titulo': 'Faja', 'costo': 9000},
        'MLA2': {'titulo': 'Cortador', 'costo': 13000}}
    costos.guardar('MLA3', 2500, titulo='Parches')
    assert set(espejo(entorno)) == {'MLA1', 'MLA2', 'MLA3'}


def test_si_la_migracion_falla_no_se_escribe_nada(entorno, monkeypatch):
    entorno[os.path.normpath(costos.ESPEJO)] = {'MLA1': {'costo': 9000}}
    import modules.contabilidad_cierre as cc
    monkeypatch.setattr(cc, 'importar_costos_del_sistema', lambda: {'error': 'sin base'})
    with pytest.raises(RuntimeError):
        costos.guardar('MLA2', 100)
    assert espejo(entorno) == {'MLA1': {'costo': 9000}}


def test_primer_costo_rige_desde_enero_y_el_cambio_desde_hoy(entorno):
    costos.unificar_una_vez()
    costos.guardar('MLA1', 9000)
    costos.guardar('MLA1', 11000, desde=HOY + timedelta(days=1))
    from web.db import session_scope
    from modules.contabilidad_cierre import costo_vigente
    with session_scope() as s:
        assert float(costo_vigente(s, 'MLA1', date(HOY.year, 1, 2)).costo_unitario) == 9000
        assert float(costo_vigente(s, 'MLA1', HOY + timedelta(days=2)).costo_unitario) == 11000
    assert costos.vigentes()['MLA1']['costo'] == 9000.0       # hoy todavía rige el viejo


def test_corregir_el_mismo_dia_pisa_en_vez_de_duplicar(entorno):
    costos.unificar_una_vez()
    costos.guardar('MLA1', 9000)
    costos.guardar('MLA1', 9500)                               # mismo día que la 2da fila
    costos.guardar('MLA1', 9600)
    from web.db import session_scope
    from web.models_contabilidad import CostoProducto
    with session_scope() as s:
        assert s.query(CostoProducto).filter_by(item_id='MLA1').count() == 2
    assert costos.vigentes()['MLA1']['costo'] == 9600.0
    assert espejo(entorno)['MLA1']['costo'] == 9600.0


def test_borrar_saca_el_costo_del_espejo(entorno):
    costos.unificar_una_vez()
    costos.guardar('MLA1', 9000)
    costos.borrar('MLA1')
    assert 'MLA1' not in espejo(entorno)


def test_costo_invalido(entorno):
    costos.unificar_una_vez()
    with pytest.raises(ValueError):
        costos.guardar('MLA1', 0)


def test_costos_distintos_quedan_registrados_y_gana_la_tabla(entorno):
    from web.db import session_scope
    from web.models_contabilidad import CostoProducto
    with session_scope() as s:
        s.add(CostoProducto(item_id='MLA1', variacion='', costo_unitario=12000,
                            vigente_desde=date(HOY.year, 1, 1), origen_dato='manual'))
    entorno[os.path.normpath(costos.ESPEJO)] = {'MLA1': {'titulo': 'Faja', 'costo': 9000}}
    res = costos.unificar_una_vez()
    assert res['distintos'] == [{'item_id': 'MLA1', 'titulo': 'Faja', 'costo_json': 9000.0, 'costo_tabla': 12000.0}]
    assert espejo(entorno)['MLA1']['costo'] == 12000.0


def test_borrar_desde_el_panel_no_toca_costos_contables(entorno):
    # Hallazgo de la revisión de seguridad: vaciar el campo en Mis
    # publicaciones borraba la historia contable y cambiaba meses cerrados.
    costos.unificar_una_vez()
    from web.db import session_scope
    from web.models_contabilidad import CostoProducto
    with session_scope() as s:
        s.add(CostoProducto(item_id='MLA9', variacion='', costo_unitario=5000,
                            vigente_desde=date(HOY.year, 1, 1), origen_dato='excel'))
    costos.sincronizar_espejo()
    costos.guardar('MLA9', 5200, origen='panel')               # versión de hoy desde el panel
    assert costos.borrar('MLA9') == 1                          # se va solo la del panel
    assert espejo(entorno)['MLA9']['costo'] == 5000.0          # queda la contable


def test_limpiar_demo_solo_toca_el_seed():
    # Antes también marcaba "huérfanos" (ítems fuera del stock actual: pausados,
    # terminados u otra cuenta) y con costos unificados eso borraba contabilidad.
    # Se carga la función sola: importar web.app arranca el scheduler.
    src = open(os.path.join(os.path.dirname(__file__), '..', 'web', 'app.py')).read()
    i = src.index('def _detectar_costos_demo')
    ns = {}
    exec(src[i:src.index('\n\n\n', i)], ns)
    detectar = ns['_detectar_costos_demo']
    costos_json = {'MLA001': {}, 'MLA099': {}, 'MLA2570796766': {}, 'MLA1234': {}}
    assert detectar(costos_json, [{'id': 'MLA1234'}]) == ['MLA001', 'MLA099']
