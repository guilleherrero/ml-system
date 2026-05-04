"""
Tests del módulo veredicto_optimizacion (Sprint 3.2).

Cubre:
  - Safeguard de tiempo (<7 días → data_insuficiente)
  - Safeguard de snapshots (<3 → data_insuficiente)
  - Safeguard de idempotencia (ya existe vigente → ya_existe)
  - Override force=True
  - Threshold dinámico (snapshots<5 → 20%, ≥5 → 15%)
  - Cálculo de deltas (baseline v2 + snapshot ligero)
  - Fingerprint idempotente
  - listar_pendientes con filtros
  - Persistencia (descartar, marcar obsoleto)

NO llama a Claude API — _generar_veredicto_ia se monkey-patchea con stub.

Ejecución:
    python3 tests/test_veredicto_optimizacion.py
    o
    python3 -m unittest tests.test_veredicto_optimizacion
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

# Agregar root del proyecto al path para `import modules.X`
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from modules import veredicto_optimizacion as vo


# ── Helpers ──────────────────────────────────────────────────────────────────

def _hace_dias(n: int) -> str:
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


def _baseline_v2_completo() -> dict:
    """Baseline v2 con todas las secciones — el que produce baseline_capture."""
    return {
        "fecha":        "2026-04-25 10:00",
        "version":      2,
        "visitas_7d":   100,        # legacy en root
        "ventas_30d":   8,
        "ventas_total": 480,
        "conv_pct":     2.5,
        "posicion":     12,
        "posicion_kw":  "lampara led",
        "visibilidad": {
            "score_calidad_ml":      65,
            "esta_en_catalogo":      True,
            "tiene_buy_box":         False,
            "posicion_top_keywords": [{"kw": "lampara led", "posicion": 12}],
        },
        "trafico": {
            "visitas_7d":    100,
            "visitas_30d":   430,
            "conversion_7d": 2.0,
            "conversion_30d": 2.5,
        },
        "ventas": {
            "unidades_7d":             2,
            "unidades_30d":            8,
            "facturacion_7d":          16000,
            "facturacion_30d":         64000,
            "ticket_promedio":         8000,
            "ventas_total_historica":  480,
        },
        "engagement": {"preguntas_pendientes": 0, "resenas_cantidad": 12},
        "salud":      {"reclamos_30d": 0, "cancelaciones_30d": 0},
        "_unavailable": [],
        "_capture_metrics": {"ml_api_calls_count": 14, "duration_ms": 4200},
    }


def _snapshot_ligero(visitas_7d: int, ventas_30d: int, ventas_total: int,
                     conv_pct: float, posicion: int, fecha: str | None = None) -> dict:
    """Snapshot estilo /api/monitor-refresh — campos planos en root."""
    return {
        "fecha":        fecha or datetime.now().strftime("%Y-%m-%d %H:%M"),
        "visitas_7d":   visitas_7d,
        "ventas_30d":   ventas_30d,
        "ventas_total": ventas_total,
        "conv_pct":     conv_pct,
        "posicion":     posicion,
    }


def _build_monitor(items: list[dict]) -> dict:
    return {"items": items}


# ── Base test class con tmpdir + monkey-patch del db_storage ─────────────────

class VeredictoTestBase(unittest.TestCase):
    """Setea tmpdir como data_dir y fuerza db_storage a usar filesystem."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = self.tmp.name
        # Forzar filesystem (sin DATABASE_URL → db_storage usa FS)
        self._old_db_url = os.environ.pop("DATABASE_URL", None)
        # Resetear conexión cacheada del db_storage
        from core import db_storage
        db_storage._db_url = ""
        db_storage._conn = None

    def tearDown(self):
        if self._old_db_url is not None:
            os.environ["DATABASE_URL"] = self._old_db_url
        self.tmp.cleanup()

    def _escribir_monitor(self, items: list[dict]) -> None:
        path = os.path.join(self.data_dir, "monitor_evolucion.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(_build_monitor(items), f)


# ── Tests ────────────────────────────────────────────────────────────────────

class TestThreshold(unittest.TestCase):

    def test_threshold_data_ruidosa(self):
        # <5 snapshots → 20%
        self.assertEqual(vo.threshold_aplicado(0), 20.0)
        self.assertEqual(vo.threshold_aplicado(3), 20.0)
        self.assertEqual(vo.threshold_aplicado(4), 20.0)

    def test_threshold_default(self):
        # ≥5 snapshots → 15%
        self.assertEqual(vo.threshold_aplicado(5), 15.0)
        self.assertEqual(vo.threshold_aplicado(7), 15.0)
        self.assertEqual(vo.threshold_aplicado(30), 15.0)


class TestFingerprint(unittest.TestCase):

    def test_fingerprint_idempotente(self):
        fp1 = vo.fingerprint_veredicto("Novara", "MLA123", "2026-04-25")
        fp2 = vo.fingerprint_veredicto("Novara", "MLA123", "2026-04-25")
        self.assertEqual(fp1, fp2)
        self.assertEqual(len(fp1), 16)   # sha1 truncado a 16 hex

    def test_fingerprint_distintos_si_cambia_input(self):
        fp_base = vo.fingerprint_veredicto("Novara", "MLA123", "2026-04-25")
        # Cambiar cualquier campo da fp distinto
        self.assertNotEqual(fp_base, vo.fingerprint_veredicto("Otra",   "MLA123", "2026-04-25"))
        self.assertNotEqual(fp_base, vo.fingerprint_veredicto("Novara", "MLA999", "2026-04-25"))
        self.assertNotEqual(fp_base, vo.fingerprint_veredicto("Novara", "MLA123", "2026-04-26"))


class TestDeltas(unittest.TestCase):

    def test_deltas_baseline_v2_vs_snapshot_ligero(self):
        b = _baseline_v2_completo()
        # Snapshot mejorando: visitas 100→140 (+40%), ventas 8→12 (+50%)
        s = _snapshot_ligero(visitas_7d=140, ventas_30d=12, ventas_total=520,
                             conv_pct=3.5, posicion=7)
        d = vo._calcular_deltas(b, s)
        self.assertEqual(d["visitas_7d"]["t0"], 100.0)
        self.assertEqual(d["visitas_7d"]["actual"], 140.0)
        self.assertEqual(d["visitas_7d"]["delta_pct"], 40.0)
        self.assertEqual(d["ventas_30d"]["delta_pct"], 50.0)
        # Posición: T0=12, actual=7 → mejoró 5 puestos. delta_abs=12-7=5 (positivo=mejoró)
        self.assertEqual(d["posicion"]["delta_abs"], 5.0)
        self.assertIsNone(d["posicion"]["delta_pct"])

    def test_deltas_baseline_legacy_v1(self):
        # Baseline sin secciones — solo campos planos en root
        b = {
            "fecha": "2026-04-25", "version": 1,
            "visitas_7d": 50, "ventas_30d": 4, "ventas_total": 100,
            "conv_pct": 1.0, "posicion": 20,
        }
        s = _snapshot_ligero(visitas_7d=60, ventas_30d=5, ventas_total=105,
                             conv_pct=1.2, posicion=18)
        d = vo._calcular_deltas(b, s)
        self.assertEqual(d["visitas_7d"]["t0"], 50.0)
        self.assertEqual(d["visitas_7d"]["delta_pct"], 20.0)
        self.assertEqual(d["posicion"]["delta_abs"], 2.0)   # mejoró 2 puestos

    def test_deltas_t0_cero_no_explota(self):
        # T0=0 + actual>0 debe dar 999 (señal de mejora extrema)
        b = {"version": 1, "visitas_7d": 0, "ventas_30d": 0,
             "ventas_total": 0, "conv_pct": 0, "posicion": None}
        s = _snapshot_ligero(visitas_7d=10, ventas_30d=2, ventas_total=2,
                             conv_pct=20.0, posicion=5)
        d = vo._calcular_deltas(b, s)
        self.assertEqual(d["visitas_7d"]["delta_pct"], 999.0)
        self.assertEqual(d["ventas_30d"]["delta_pct"], 999.0)

    def test_deltas_actual_none_devuelve_none(self):
        # Si el snapshot no tiene la métrica, delta_pct queda None
        b = _baseline_v2_completo()
        s = {"fecha": "2026-05-04 10:00"}   # snapshot vacío
        d = vo._calcular_deltas(b, s)
        for k in ("visitas_7d", "ventas_30d", "ventas_total", "conv_pct"):
            self.assertIsNone(d[k]["delta_pct"], f"esperaba None en {k}")


class TestSafeguards(VeredictoTestBase):

    def test_no_encontrado_si_no_hay_entry(self):
        self._escribir_monitor([])
        r = vo.evaluar_optimizacion("MLA999", "Novara", self.data_dir)
        self.assertEqual(r["estado"], "no_encontrado")
        self.assertIsNone(r["veredicto"])

    def test_data_insuficiente_si_menos_de_7_dias(self):
        # Optimización aplicada hace 3 días
        self._escribir_monitor([{
            "alias": "Novara", "item_id": "MLA1", "titulo_producto": "Producto",
            "fecha_opt": _hace_dias(3), "baseline": _baseline_v2_completo(),
            "snapshots": [_snapshot_ligero(110, 9, 490, 2.6, 11),
                          _snapshot_ligero(115, 10, 495, 2.7, 10)],
        }])
        r = vo.evaluar_optimizacion("MLA1", "Novara", self.data_dir)
        self.assertEqual(r["estado"], "data_insuficiente")
        self.assertEqual(r["dias_transcurridos"], 3)
        self.assertEqual(r["dias_faltantes"], 4)
        self.assertIn("proxima_evaluacion", r)
        self.assertIsNone(r["veredicto"])

    def test_data_insuficiente_si_menos_de_3_snapshots(self):
        # Pasaron 7 días pero solo 2 snapshots
        self._escribir_monitor([{
            "alias": "Novara", "item_id": "MLA1", "titulo_producto": "X",
            "fecha_opt": _hace_dias(8), "baseline": _baseline_v2_completo(),
            "snapshots": [_snapshot_ligero(110, 9, 490, 2.6, 11),
                          _snapshot_ligero(115, 10, 495, 2.7, 10)],
        }])
        r = vo.evaluar_optimizacion("MLA1", "Novara", self.data_dir)
        self.assertEqual(r["estado"], "data_insuficiente")
        self.assertEqual(r["snapshots_usados"], 2)
        self.assertIsNone(r["veredicto"])

    def test_force_no_salta_check_de_7_dias(self):
        # force=True NO salta el safeguard de tiempo
        self._escribir_monitor([{
            "alias": "Novara", "item_id": "MLA1", "titulo_producto": "X",
            "fecha_opt": _hace_dias(2), "baseline": _baseline_v2_completo(),
            "snapshots": [_snapshot_ligero(110, 9, 490, 2.6, 11)] * 5,
        }])
        r = vo.evaluar_optimizacion("MLA1", "Novara", self.data_dir, force=True)
        self.assertEqual(r["estado"], "data_insuficiente")
        self.assertIsNone(r["veredicto"])

    def test_data_insuficiente_si_baseline_vacio(self):
        # entry sin baseline (capturándose)
        self._escribir_monitor([{
            "alias": "Novara", "item_id": "MLA1", "titulo_producto": "X",
            "fecha_opt": _hace_dias(10), "baseline": {},
            "snapshots": [_snapshot_ligero(110, 9, 490, 2.6, 11)] * 5,
            "_capturing": True,
        }])
        r = vo.evaluar_optimizacion("MLA1", "Novara", self.data_dir)
        self.assertEqual(r["estado"], "data_insuficiente")


class TestFlujoCompletoConStub(VeredictoTestBase):
    """Tests que llegan a generar veredicto — _generar_veredicto_ia stub-eado."""

    def setUp(self):
        super().setUp()
        # Monkey-patch del generador IA para no llamar a Claude
        self._orig_gen = vo._generar_veredicto_ia
        vo._generar_veredicto_ia = self._fake_gen

    def tearDown(self):
        vo._generar_veredicto_ia = self._orig_gen
        super().tearDown()

    def _fake_gen(self, **kwargs) -> dict:
        """Stub que devuelve un veredicto con estructura final esperada."""
        return {
            "fingerprint":         vo.fingerprint_veredicto(
                                       kwargs["alias"], kwargs["item_id"], kwargs["fecha_opt"]),
            "alias":               kwargs["alias"],
            "item_id":             kwargs["item_id"],
            "titulo_producto":     kwargs["titulo_producto"],
            "fecha_optimizacion":  kwargs["fecha_opt"],
            "fecha_veredicto":     datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "dias_transcurridos":  kwargs["dias"],
            "veredicto":           "ganadora",
            "score_exito":         78,
            "razonamiento":        "stub: visitas +40%, ventas +50%, posición -5",
            "recomendacion":       "replicar",
            "razon_recomendacion": "stub",
            "metricas_clave":      ["visitas_7d", "ventas_30d"],
            "alertas":             [],
            "deltas":              kwargs["deltas"],
            "snapshots_usados":    kwargs["snapshots_usados"],
            "estado":              "vigente",
            "_debug": {
                "timestamp":         datetime.now().isoformat(timespec="seconds"),
                "tokens_in":         2840,
                "tokens_out":        920,
                "cost_usd":          0.111,
                "threshold_aplicado": kwargs["threshold"],
                "snapshots_usados":   kwargs["snapshots_usados"],
                "snapshots_disponibles": kwargs["snapshots_usados"],
                "prompt_version":    vo.PROMPT_VERSION,
                "modelo":            vo.MODELO_DEFAULT,
            },
        }

    def test_genera_y_persiste_veredicto(self):
        self._escribir_monitor([{
            "alias": "Novara", "item_id": "MLA1", "titulo_producto": "Lampara LED",
            "fecha_opt": _hace_dias(8), "baseline": _baseline_v2_completo(),
            "snapshots": [_snapshot_ligero(110+i, 8+i, 480+i, 2.5+i*0.1, 12-i)
                          for i in range(7)],
            "ultimo_snapshot": _snapshot_ligero(140, 12, 520, 3.5, 7),
        }])
        r = vo.evaluar_optimizacion("MLA1", "Novara", self.data_dir)
        self.assertEqual(r["estado"], "veredicto")
        v = r["veredicto"]
        self.assertEqual(v["veredicto"], "ganadora")
        self.assertEqual(v["estado"], "vigente")
        # Persistido en disco
        path = os.path.join(self.data_dir, "veredictos_optimizacion.json")
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            store = json.load(f)
        self.assertEqual(len(store["items"]), 1)
        self.assertEqual(store["items"][0]["fingerprint"], v["fingerprint"])
        # _debug presente
        self.assertIn("_debug", v)
        self.assertEqual(v["_debug"]["prompt_version"], vo.PROMPT_VERSION)
        self.assertEqual(v["_debug"]["modelo"], vo.MODELO_DEFAULT)

    def test_threshold_dinamico_segun_snapshots(self):
        # Caso A: 4 snapshots → threshold 20%
        self._escribir_monitor([{
            "alias": "N", "item_id": "MLA1", "titulo_producto": "X",
            "fecha_opt": _hace_dias(8), "baseline": _baseline_v2_completo(),
            "snapshots": [_snapshot_ligero(110, 9, 490, 2.6, 11)] * 4,
            "ultimo_snapshot": _snapshot_ligero(110, 9, 490, 2.6, 11),
        }])
        r = vo.evaluar_optimizacion("MLA1", "N", self.data_dir)
        self.assertEqual(r["veredicto"]["_debug"]["threshold_aplicado"], 20.0)

        # Caso B: 7 snapshots → threshold 15%. Otra optimización para evitar ya_existe.
        self._escribir_monitor([{
            "alias": "N", "item_id": "MLA2", "titulo_producto": "Y",
            "fecha_opt": _hace_dias(10), "baseline": _baseline_v2_completo(),
            "snapshots": [_snapshot_ligero(110, 9, 490, 2.6, 11)] * 7,
            "ultimo_snapshot": _snapshot_ligero(110, 9, 490, 2.6, 11),
        }])
        r = vo.evaluar_optimizacion("MLA2", "N", self.data_dir)
        self.assertEqual(r["veredicto"]["_debug"]["threshold_aplicado"], 15.0)

    def test_ya_existe_si_vigente(self):
        self._escribir_monitor([{
            "alias": "N", "item_id": "MLA1", "titulo_producto": "X",
            "fecha_opt": _hace_dias(8), "baseline": _baseline_v2_completo(),
            "snapshots": [_snapshot_ligero(110, 9, 490, 2.6, 11)] * 5,
            "ultimo_snapshot": _snapshot_ligero(110, 9, 490, 2.6, 11),
        }])
        # Generar la primera vez
        r1 = vo.evaluar_optimizacion("MLA1", "N", self.data_dir)
        self.assertEqual(r1["estado"], "veredicto")
        # Volver a llamar — debe devolver ya_existe
        r2 = vo.evaluar_optimizacion("MLA1", "N", self.data_dir)
        self.assertEqual(r2["estado"], "ya_existe")
        self.assertEqual(r2["veredicto"]["fingerprint"], r1["veredicto"]["fingerprint"])

    def test_force_regenera_y_marca_anterior_obsoleto(self):
        self._escribir_monitor([{
            "alias": "N", "item_id": "MLA1", "titulo_producto": "X",
            "fecha_opt": _hace_dias(8), "baseline": _baseline_v2_completo(),
            "snapshots": [_snapshot_ligero(110, 9, 490, 2.6, 11)] * 5,
            "ultimo_snapshot": _snapshot_ligero(110, 9, 490, 2.6, 11),
        }])
        vo.evaluar_optimizacion("MLA1", "N", self.data_dir)
        r2 = vo.evaluar_optimizacion("MLA1", "N", self.data_dir, force=True)
        self.assertEqual(r2["estado"], "veredicto")
        # El store debería tener exactamente 1 item porque el fingerprint es el
        # mismo (alias+item+fecha_opt no cambian → reemplaza in-place).
        path = os.path.join(self.data_dir, "veredictos_optimizacion.json")
        with open(path) as f:
            store = json.load(f)
        vigentes = [v for v in store["items"] if v["estado"] == "vigente"]
        self.assertEqual(len(vigentes), 1, f"Esperaba 1 vigente, hay {len(vigentes)}")

    def test_descartar_marca_estado(self):
        self._escribir_monitor([{
            "alias": "N", "item_id": "MLA1", "titulo_producto": "X",
            "fecha_opt": _hace_dias(8), "baseline": _baseline_v2_completo(),
            "snapshots": [_snapshot_ligero(110, 9, 490, 2.6, 11)] * 5,
            "ultimo_snapshot": _snapshot_ligero(110, 9, 490, 2.6, 11),
        }])
        vo.evaluar_optimizacion("MLA1", "N", self.data_dir)
        ok = vo.descartar_veredicto("MLA1", "N", self.data_dir, razon="test")
        self.assertTrue(ok)
        v = vo.obtener_veredicto("MLA1", "N", self.data_dir)
        self.assertIsNone(v, "No debería haber veredicto vigente luego de descartar")
        v_inc = vo.obtener_veredicto("MLA1", "N", self.data_dir, incluir_no_vigentes=True)
        self.assertEqual(v_inc["estado"], "descartado")
        self.assertEqual(v_inc["razon_descarte"], "test")


class TestListarPendientes(VeredictoTestBase):

    def test_pendientes_filtra_correctamente(self):
        self._escribir_monitor([
            # 1) Cumple TODO → pendiente
            {"alias": "N", "item_id": "MLA1", "titulo_producto": "OK",
             "fecha_opt": _hace_dias(10), "baseline": _baseline_v2_completo(),
             "snapshots": [_snapshot_ligero(100, 8, 480, 2.5, 12)] * 5},
            # 2) Solo 5 días → no pendiente
            {"alias": "N", "item_id": "MLA2", "titulo_producto": "Joven",
             "fecha_opt": _hace_dias(5), "baseline": _baseline_v2_completo(),
             "snapshots": [_snapshot_ligero(100, 8, 480, 2.5, 12)] * 5},
            # 3) Sin baseline → no pendiente
            {"alias": "N", "item_id": "MLA3", "titulo_producto": "Sin baseline",
             "fecha_opt": _hace_dias(10), "baseline": {},
             "snapshots": [_snapshot_ligero(100, 8, 480, 2.5, 12)] * 5},
            # 4) Sin snapshots suficientes → no pendiente
            {"alias": "N", "item_id": "MLA4", "titulo_producto": "Sin snaps",
             "fecha_opt": _hace_dias(10), "baseline": _baseline_v2_completo(),
             "snapshots": [_snapshot_ligero(100, 8, 480, 2.5, 12)] * 2},
            # 5) Capturando → no pendiente
            {"alias": "N", "item_id": "MLA5", "titulo_producto": "Cap",
             "fecha_opt": _hace_dias(10), "baseline": _baseline_v2_completo(),
             "snapshots": [_snapshot_ligero(100, 8, 480, 2.5, 12)] * 5,
             "_capturing": True},
        ])
        pendientes = vo.listar_pendientes(self.data_dir)
        ids = [p["item_id"] for p in pendientes]
        self.assertEqual(ids, ["MLA1"], f"Esperaba [MLA1], obtuve {ids}")
        self.assertEqual(pendientes[0]["dias"], 10)
        self.assertEqual(pendientes[0]["snapshots_n"], 5)


# ── Runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Output verbose para que tests/run_regresion.sh muestre cada caso
    unittest.main(verbosity=2)
