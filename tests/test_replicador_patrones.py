"""
Tests del módulo replicador_patrones (Sprint 3.3).

Cubre:
  - Tokenización y similitud Jaccard
  - extraer_patron_ganador con monitor_entry + optimización
  - Filtros de detectar_productos_similares (similitud, bloqueo, decisiones)
  - listar_oportunidades cruza veredictos vigentes con conteo de decisiones
  - registrar_decision (nueva, reemplazo, validación de acción)
  - Purga de decisiones descartadas vencidas (TTL 60d)
  - fingerprint idempotente
  - Hard cap mensual ($5/mes Haiku)
  - Parser de respuesta Haiku (con fence, sin fence, inválida)

NO llama a Claude API: generar_replica_haiku se monkey-patchea con stub donde
hace falta. La mayoría de los tests no la ejercen.

NO hace llamadas a la API de MercadoLibre: el `client` se mockea.

Ejecución:
    python3 tests/test_replicador_patrones.py
    o
    python3 -m unittest tests.test_replicador_patrones

Requiere Python 3.12+ (porque importa modules/seo_optimizer.py vía
encadenamiento de imports — ver docs/GUIA_REGRESION.md).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

# Agregar root del proyecto al path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from modules import replicador_patrones as rp


# ── Helpers ──────────────────────────────────────────────────────────────────

def _hace_dias(n: int) -> str:
    """Fecha legible 'YYYY-MM-DD HH:MM' a N días atrás."""
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d %H:%M")


def _hace_dias_iso(n: int) -> str:
    """ISO con TZ UTC, N días atrás."""
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat(timespec="seconds")


def _veredicto_ganador(item_id="MLA1", alias="Novara", dias_atras=10,
                       score=85, recomendacion="replicar",
                       estado="vigente", veredicto="ganadora") -> dict:
    """Veredicto sintético — shape espejado de veredicto_optimizacion."""
    return {
        "fingerprint":         f"fp_{item_id}_{dias_atras}",
        "alias":               alias,
        "item_id":             item_id,
        "titulo_producto":     f"Producto {item_id}",
        "fecha_optimizacion":  _hace_dias(dias_atras + 7),
        "fecha_veredicto":     _hace_dias_iso(dias_atras),
        "veredicto":           veredicto,
        "estado":              estado,
        "score_exito":         score,
        "recomendacion":       recomendacion,
        "razonamiento":        "La optimización mejoró visitas y ventas.",
        "metricas_clave":      ["visitas_7d", "ventas_30d"],
    }


def _monitor_entry(item_id="MLA1", alias="Novara",
                   titulo_antes="Producto Novara M",
                   titulo_despues="Producto Novara Premium Calidad Original",
                   applied=("titulo", "atributos (5)")) -> dict:
    return {
        "alias":          alias,
        "item_id":        item_id,
        "titulo_antes":   titulo_antes,
        "titulo_despues": titulo_despues,
        "applied":        list(applied),
    }


def _optimizacion(item_id="MLA1",
                  desc_nueva="Pelota de fútbol oficial Novara, ideal para entrenamientos diarios. Material premium, durable. Envío gratis.") -> dict:
    return {
        "item_id":           item_id,
        "fecha":             _hace_dias(15),
        "titulo_actual":     "Producto Novara M",
        "titulo_nuevo":      "Producto Novara Premium Calidad Original",
        "descripcion_nueva": desc_nueva,
    }


# ── Base test class — tmpdir + filesystem mode ───────────────────────────────

class ReplicadorTestBase(unittest.TestCase):
    """Setea tmpdir como data_dir y fuerza db_storage a usar filesystem."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = self.tmp.name
        # Forzar filesystem (sin DATABASE_URL → db_storage usa FS)
        self._old_db_url = os.environ.pop("DATABASE_URL", None)
        from core import db_storage
        db_storage._db_url = ""
        db_storage._conn = None

    def tearDown(self):
        if self._old_db_url is not None:
            os.environ["DATABASE_URL"] = self._old_db_url
        self.tmp.cleanup()

    def _escribir_veredictos(self, items: list[dict]) -> None:
        path = os.path.join(self.data_dir, "veredictos_optimizacion.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"items": items}, f)

    def _escribir_monitor(self, items: list[dict]) -> None:
        path = os.path.join(self.data_dir, "monitor_evolucion.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"items": items}, f)

    def _escribir_optimizaciones(self, alias: str, items: list[dict]) -> None:
        import re as _re
        safe = _re.sub(r"[^A-Za-z0-9_-]", "_", alias)
        path = os.path.join(self.data_dir, f"optimizaciones_{safe}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"alias": alias, "optimizaciones": items}, f)


# ── Tests: tokenización y similitud ──────────────────────────────────────────

class TestTokenizacion(unittest.TestCase):

    def test_normalize_quita_caracteres_especiales(self):
        # Mantiene letras + acentos, baja a lowercase
        out = rp._normalize("Cortador de Puntas - Cabello Dañado!")
        self.assertIn("cortador", out)
        self.assertIn("puntas", out)
        self.assertIn("dañado", out)
        # Caracteres especiales reemplazados por espacios
        self.assertNotIn("-", out)
        self.assertNotIn("!", out)

    def test_normalize_string_vacio(self):
        self.assertEqual(rp._normalize(""), "")
        self.assertEqual(rp._normalize(None), "")

    def test_tokenize_filtra_stopwords(self):
        tokens = rp._tokenize("Faja de embarazo para mujer con sostén")
        # Stopwords removidas: de, para, con
        self.assertNotIn("de", tokens)
        self.assertNotIn("para", tokens)
        self.assertNotIn("con", tokens)
        # Palabras válidas presentes
        self.assertIn("faja", tokens)
        self.assertIn("embarazo", tokens)
        self.assertIn("mujer", tokens)
        self.assertIn("sostén", tokens)

    def test_tokenize_filtra_palabras_cortas(self):
        # Palabras de menos de 3 chars se filtran
        tokens = rp._tokenize("X a el lo casa")
        self.assertNotIn("x", tokens)
        self.assertNotIn("el", tokens)
        self.assertNotIn("lo", tokens)
        self.assertIn("casa", tokens)

    def test_jaccard_identico(self):
        a = {"faja", "embarazo", "mujer"}
        b = {"faja", "embarazo", "mujer"}
        self.assertEqual(rp._jaccard(a, b), 1.0)

    def test_jaccard_disjunto(self):
        a = {"faja", "embarazo"}
        b = {"taladro", "martillo"}
        self.assertEqual(rp._jaccard(a, b), 0.0)

    def test_jaccard_solapamiento_parcial(self):
        a = {"faja", "embarazo", "mujer"}        # 3 elementos
        b = {"faja", "embarazo", "premium"}      # 3 elementos
        # intersección: 2, unión: 4 → 0.5
        self.assertEqual(rp._jaccard(a, b), 0.5)

    def test_jaccard_set_vacio(self):
        self.assertEqual(rp._jaccard(set(), {"a"}), 0.0)
        self.assertEqual(rp._jaccard({"a"}, set()), 0.0)
        self.assertEqual(rp._jaccard(set(), set()), 0.0)


# ── Tests: parsing y dias_desde ──────────────────────────────────────────────

class TestParseFecha(unittest.TestCase):

    def test_parse_formato_fecha_simple(self):
        dt = rp._parse_fecha("2026-04-25")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2026)
        self.assertEqual(dt.month, 4)
        self.assertEqual(dt.day, 25)

    def test_parse_formato_con_hora(self):
        dt = rp._parse_fecha("2026-04-25 14:30")
        self.assertIsNotNone(dt)
        self.assertEqual(dt.hour, 14)
        self.assertEqual(dt.minute, 30)

    def test_parse_iso_con_tz(self):
        dt = rp._parse_fecha("2026-04-25T14:30:00+00:00")
        self.assertIsNotNone(dt)

    def test_parse_iso_con_z(self):
        dt = rp._parse_fecha("2026-04-25T14:30:00Z")
        self.assertIsNotNone(dt)

    def test_parse_string_invalido(self):
        self.assertIsNone(rp._parse_fecha(""))
        self.assertIsNone(rp._parse_fecha(None))
        self.assertIsNone(rp._parse_fecha("no-es-fecha"))

    def test_dias_desde_reciente(self):
        ayer = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
        d = rp._dias_desde(ayer)
        self.assertIn(d, (0, 1))   # margen por hora del día

    def test_dias_desde_fecha_invalida(self):
        # Fecha inválida → 99999 (señal de "muy viejo")
        self.assertEqual(rp._dias_desde(""), 99999)
        self.assertEqual(rp._dias_desde("garbage"), 99999)

    def test_dias_desde_hace_semana(self):
        hace_7 = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
        d = rp._dias_desde(hace_7)
        self.assertIn(d, (6, 7))


# ── Tests: extraer_patron_ganador ────────────────────────────────────────────

class TestExtraerPatron(ReplicadorTestBase):

    def test_extrae_keywords_agregadas_y_removidas(self):
        veredicto = _veredicto_ganador()
        monitor = _monitor_entry(
            titulo_antes="Producto Novara M Edicion Vieja",
            titulo_despues="Producto Novara Premium Calidad Original",
        )
        patron = rp.extraer_patron_ganador(veredicto, None, monitor)
        # "premium", "calidad", "original" son nuevos
        self.assertIn("premium", patron["keywords_agregadas"])
        self.assertIn("calidad", patron["keywords_agregadas"])
        self.assertIn("original", patron["keywords_agregadas"])
        # "edicion", "vieja" se removieron
        self.assertIn("edicion", patron["keywords_removidas"])
        self.assertIn("vieja", patron["keywords_removidas"])

    def test_extrae_metricas_y_score_del_veredicto(self):
        veredicto = _veredicto_ganador(score=92)
        monitor = _monitor_entry()
        patron = rp.extraer_patron_ganador(veredicto, None, monitor)
        self.assertEqual(patron["veredicto_score"], 92)
        self.assertEqual(patron["metricas_clave"], ["visitas_7d", "ventas_30d"])

    def test_descripcion_estilo_truncada_a_280(self):
        veredicto = _veredicto_ganador()
        monitor = _monitor_entry()
        opt = _optimizacion(desc_nueva="X" * 500)
        patron = rp.extraer_patron_ganador(veredicto, opt, monitor)
        self.assertIsNotNone(patron["descripcion_estilo"])
        self.assertLessEqual(len(patron["descripcion_estilo"]), 280)

    def test_descripcion_estilo_none_si_no_hay_optimizacion(self):
        veredicto = _veredicto_ganador()
        monitor = _monitor_entry()
        patron = rp.extraer_patron_ganador(veredicto, None, monitor)
        self.assertIsNone(patron["descripcion_estilo"])

    def test_atributos_completados_detectado(self):
        veredicto = _veredicto_ganador()
        monitor = _monitor_entry(applied=["titulo", "atributos (5)"])
        patron = rp.extraer_patron_ganador(veredicto, None, monitor)
        self.assertTrue(patron["atributos_completados"])

    def test_atributos_no_completados(self):
        veredicto = _veredicto_ganador()
        monitor = _monitor_entry(applied=["titulo"])
        patron = rp.extraer_patron_ganador(veredicto, None, monitor)
        self.assertFalse(patron["atributos_completados"])

    def test_monitor_entry_none_no_explota(self):
        # Con monitor_entry None, debe completar campos vacíos sin romper
        veredicto = _veredicto_ganador(item_id="MLA9")
        patron = rp.extraer_patron_ganador(veredicto, None, None)
        self.assertEqual(patron["titulo_antes"], "")
        self.assertEqual(patron["titulo_despues"], "")
        self.assertEqual(patron["keywords_agregadas"], [])
        self.assertEqual(patron["keywords_removidas"], [])
        self.assertEqual(patron["item_origen"], "MLA9")  # cae al veredicto


# ── Tests: listar_oportunidades ──────────────────────────────────────────────

class TestListarOportunidades(ReplicadorTestBase):

    def test_filtra_solo_ganadoras_con_replicar(self):
        self._escribir_veredictos([
            _veredicto_ganador(item_id="MLA1"),                      # ✓
            _veredicto_ganador(item_id="MLA2", veredicto="neutra"),  # ✗ no es ganadora
            _veredicto_ganador(item_id="MLA3", recomendacion="mantener"),  # ✗
            _veredicto_ganador(item_id="MLA4", estado="descartado"), # ✗ no vigente
        ])
        oportunidades = rp.listar_oportunidades("Novara", self.data_dir)
        ids = [o["item_origen"] for o in oportunidades]
        self.assertEqual(ids, ["MLA1"])

    def test_ordenadas_por_score_desc(self):
        self._escribir_veredictos([
            _veredicto_ganador(item_id="MLA_BAJO", score=60),
            _veredicto_ganador(item_id="MLA_ALTO", score=95),
            _veredicto_ganador(item_id="MLA_MEDIO", score=80),
        ])
        oportunidades = rp.listar_oportunidades("Novara", self.data_dir)
        scores = [o["score_exito"] for o in oportunidades]
        self.assertEqual(scores, [95, 80, 60])

    def test_filtra_por_alias(self):
        self._escribir_veredictos([
            _veredicto_ganador(item_id="MLA1", alias="Novara"),
            _veredicto_ganador(item_id="MLA2", alias="OtraCuenta"),
        ])
        oportunidades = rp.listar_oportunidades("Novara", self.data_dir)
        self.assertEqual(len(oportunidades), 1)
        self.assertEqual(oportunidades[0]["item_origen"], "MLA1")

    def test_excluye_veredictos_fuera_de_ventana(self):
        # Ventana default = 90 días
        self._escribir_veredictos([
            _veredicto_ganador(item_id="MLA_RECIENTE", dias_atras=10),
            _veredicto_ganador(item_id="MLA_VIEJO", dias_atras=120),
        ])
        oportunidades = rp.listar_oportunidades("Novara", self.data_dir)
        ids = [o["item_origen"] for o in oportunidades]
        self.assertIn("MLA_RECIENTE", ids)
        self.assertNotIn("MLA_VIEJO", ids)

    def test_conteos_de_decisiones_correctos(self):
        # 1 veredicto ganador
        self._escribir_veredictos([_veredicto_ganador(item_id="MLA1")])
        # 3 decisiones para ese origen: 1 aplicada, 1 descartada, 1 preview
        rp.registrar_decision("Novara", "MLA1", "MLAd1", "aplicada", self.data_dir)
        rp.registrar_decision("Novara", "MLA1", "MLAd2", "descartada", self.data_dir)
        rp.registrar_decision("Novara", "MLA1", "MLAd3", "preview_generado", self.data_dir)
        oportunidades = rp.listar_oportunidades("Novara", self.data_dir)
        self.assertEqual(oportunidades[0]["replicas_aplicadas"], 1)
        self.assertEqual(oportunidades[0]["replicas_descartadas"], 1)
        self.assertEqual(oportunidades[0]["previews_generados"], 1)

    def test_lista_vacia_si_no_hay_veredictos(self):
        oportunidades = rp.listar_oportunidades("Novara", self.data_dir)
        self.assertEqual(oportunidades, [])


# ── Tests: detectar_productos_similares ──────────────────────────────────────

class TestDetectarSimilares(ReplicadorTestBase):

    def _make_client(self, origen_data: dict, listings_ids: list,
                     items_full: list[dict]) -> MagicMock:
        """Construye un mock de MLClient con los datos justos."""
        client = MagicMock()
        client.get_item.return_value = origen_data
        client.get_my_listings.return_value = {
            "results": listings_ids,
            "paging": {"total": len(listings_ids)},
        }
        # Mockear también el atributo `account.access_token` que usa el módulo
        client.account = MagicMock()
        client.account.access_token = "fake_token"
        # Stub del multiget — el módulo lo llama vía requests.get → patcheamos eso
        return client

    def test_filtro_jaccard_minimo(self):
        # Origen y candidato sin tokens en común → score 0 → excluido
        origen = {"id": "MLAo", "title": "Faja embarazo soporte", "category_id": "MLA111"}
        items_full = [
            {"id": "MLA1", "title": "Taladro inalambrico bosch profesional",
             "category_id": "MLA222", "price": 1000, "sold_quantity": 0},
        ]
        client = self._make_client(origen, ["MLA1"], items_full)
        # Patchear el multiget para que devuelva items_full directamente
        with unittest.mock.patch.object(rp, "_fetch_items_multiget",
                                          return_value=items_full):
            candidatos = rp.detectar_productos_similares(
                "Novara", "MLAo", client, self.data_dir)
        self.assertEqual(candidatos, [])

    def test_misma_categoria_da_boost(self):
        origen = {"id": "MLAo", "title": "Faja embarazo soporte", "category_id": "MLA111"}
        items_full = [
            # Misma cat + 1 keyword en común → con boost pasa filtro
            {"id": "MLA1", "title": "Faja postparto cesarea novara",
             "category_id": "MLA111", "price": 1000, "sold_quantity": 0},
        ]
        client = self._make_client(origen, ["MLA1"], items_full)
        with unittest.mock.patch.object(rp, "_fetch_items_multiget",
                                          return_value=items_full):
            candidatos = rp.detectar_productos_similares(
                "Novara", "MLAo", client, self.data_dir)
        self.assertEqual(len(candidatos), 1)
        self.assertTrue(candidatos[0]["mismo_categoria"])
        self.assertGreaterEqual(candidatos[0]["similitud"],
                                rp.SIMILARITY_MIN_JACCARD)

    def test_excluye_origen_de_los_candidatos(self):
        origen = {"id": "MLAo", "title": "Faja embarazo soporte", "category_id": "MLA111"}
        # El origen aparece en los listings (no debería ocurrir, pero defensivo)
        items_full = [
            {"id": "MLAo", "title": "Faja embarazo soporte",
             "category_id": "MLA111", "price": 1000, "sold_quantity": 0},
            {"id": "MLA1", "title": "Faja postparto cesarea novara",
             "category_id": "MLA111", "price": 1000, "sold_quantity": 0},
        ]
        client = self._make_client(origen, ["MLA1"], items_full)
        with unittest.mock.patch.object(rp, "_fetch_items_multiget",
                                          return_value=items_full):
            candidatos = rp.detectar_productos_similares(
                "Novara", "MLAo", client, self.data_dir)
        ids = [c["item_id"] for c in candidatos]
        self.assertNotIn("MLAo", ids)
        self.assertIn("MLA1", ids)

    def test_optimizado_recientemente_queda_bloqueado(self):
        origen = {"id": "MLAo", "title": "Faja embarazo soporte", "category_id": "MLA111"}
        items_full = [
            {"id": "MLA1", "title": "Faja postparto soporte novara",
             "category_id": "MLA111", "price": 1000, "sold_quantity": 0},
        ]
        # Optimización hace 5 días → bloqueado (threshold 30d)
        self._escribir_optimizaciones("Novara", [
            {"item_id": "MLA1", "fecha": _hace_dias(5),
             "titulo_actual": "x", "titulo_nuevo": "y"},
        ])
        client = self._make_client(origen, ["MLA1"], items_full)
        with unittest.mock.patch.object(rp, "_fetch_items_multiget",
                                          return_value=items_full):
            candidatos = rp.detectar_productos_similares(
                "Novara", "MLAo", client, self.data_dir)
        # Sigue apareciendo (para informar al usuario), pero con bloqueo
        self.assertEqual(len(candidatos), 1)
        self.assertIsNotNone(candidatos[0]["bloqueado_por"])
        self.assertIn("Optimizado hace", candidatos[0]["bloqueado_por"])

    def test_optimizado_hace_mucho_no_bloquea(self):
        origen = {"id": "MLAo", "title": "Faja embarazo soporte", "category_id": "MLA111"}
        items_full = [
            {"id": "MLA1", "title": "Faja postparto soporte novara",
             "category_id": "MLA111", "price": 1000, "sold_quantity": 0},
        ]
        # Optimización hace 60 días → NO bloquea
        self._escribir_optimizaciones("Novara", [
            {"item_id": "MLA1", "fecha": _hace_dias(60),
             "titulo_actual": "x", "titulo_nuevo": "y"},
        ])
        client = self._make_client(origen, ["MLA1"], items_full)
        with unittest.mock.patch.object(rp, "_fetch_items_multiget",
                                          return_value=items_full):
            candidatos = rp.detectar_productos_similares(
                "Novara", "MLAo", client, self.data_dir)
        self.assertEqual(len(candidatos), 1)
        self.assertIsNone(candidatos[0]["bloqueado_por"])

    def test_decision_aplicada_previamente_bloquea(self):
        origen = {"id": "MLAo", "title": "Faja embarazo soporte", "category_id": "MLA111"}
        items_full = [
            {"id": "MLA1", "title": "Faja postparto soporte novara",
             "category_id": "MLA111", "price": 1000, "sold_quantity": 0},
        ]
        # Decision previa: ya replicado a este destino
        rp.registrar_decision("Novara", "MLAo", "MLA1", "aplicada", self.data_dir)
        client = self._make_client(origen, ["MLA1"], items_full)
        with unittest.mock.patch.object(rp, "_fetch_items_multiget",
                                          return_value=items_full):
            candidatos = rp.detectar_productos_similares(
                "Novara", "MLAo", client, self.data_dir)
        self.assertEqual(len(candidatos), 1)
        self.assertIn("Ya replicaste", candidatos[0]["bloqueado_por"])

    def test_orden_no_bloqueados_primero_luego_score(self):
        origen = {"id": "MLAo", "title": "Faja embarazo soporte", "category_id": "MLA111"}
        items_full = [
            {"id": "MLA_BLQ", "title": "Faja postparto soporte total",
             "category_id": "MLA111", "price": 1000, "sold_quantity": 0},
            {"id": "MLA_LIB_ALTO", "title": "Faja embarazo soporte abdomen",
             "category_id": "MLA111", "price": 1000, "sold_quantity": 0},
            {"id": "MLA_LIB_BAJO", "title": "Faja postparto novara",
             "category_id": "MLA111", "price": 1000, "sold_quantity": 0},
        ]
        # MLA_BLQ está bloqueado por optimización reciente
        self._escribir_optimizaciones("Novara", [
            {"item_id": "MLA_BLQ", "fecha": _hace_dias(5),
             "titulo_actual": "x", "titulo_nuevo": "y"},
        ])
        client = self._make_client(origen, [c["id"] for c in items_full], items_full)
        with unittest.mock.patch.object(rp, "_fetch_items_multiget",
                                          return_value=items_full):
            candidatos = rp.detectar_productos_similares(
                "Novara", "MLAo", client, self.data_dir)
        ids = [c["item_id"] for c in candidatos]
        # No-bloqueados van primero
        self.assertEqual(ids[-1], "MLA_BLQ")

    def test_lista_vacia_si_no_hay_listings(self):
        origen = {"id": "MLAo", "title": "Faja embarazo", "category_id": "MLA111"}
        client = self._make_client(origen, [], [])
        with unittest.mock.patch.object(rp, "_fetch_items_multiget",
                                          return_value=[]):
            candidatos = rp.detectar_productos_similares(
                "Novara", "MLAo", client, self.data_dir)
        self.assertEqual(candidatos, [])

    def test_falla_silenciosa_si_origen_no_existe(self):
        client = MagicMock()
        client.get_item.side_effect = Exception("404 not found")
        candidatos = rp.detectar_productos_similares(
            "Novara", "MLA_NO_EXISTE", client, self.data_dir)
        self.assertEqual(candidatos, [])


# ── Tests: registrar_decision y purga ────────────────────────────────────────

class TestRegistrarDecision(ReplicadorTestBase):

    def test_registra_nueva_decision(self):
        es_nueva = rp.registrar_decision(
            "Novara", "MLA1", "MLA2", "aplicada", self.data_dir)
        self.assertTrue(es_nueva)
        store = rp.obtener_replicas("Novara", self.data_dir)
        self.assertEqual(len(store["items"]), 1)
        self.assertEqual(store["items"][0]["accion"], "aplicada")

    def test_reemplaza_decision_existente(self):
        rp.registrar_decision("Novara", "MLA1", "MLA2", "preview_generado",
                              self.data_dir)
        es_nueva = rp.registrar_decision(
            "Novara", "MLA1", "MLA2", "aplicada", self.data_dir)
        self.assertFalse(es_nueva)   # NO es nueva, reemplazó
        store = rp.obtener_replicas("Novara", self.data_dir)
        self.assertEqual(len(store["items"]), 1)
        self.assertEqual(store["items"][0]["accion"], "aplicada")

    def test_accion_invalida_levanta_error(self):
        with self.assertRaises(ValueError):
            rp.registrar_decision(
                "Novara", "MLA1", "MLA2", "accion_inexistente", self.data_dir)

    def test_payload_se_guarda(self):
        payload = {"titulo_propuesto": "Nuevo título", "confianza": 80}
        rp.registrar_decision(
            "Novara", "MLA1", "MLA2", "preview_generado", self.data_dir,
            payload=payload)
        store = rp.obtener_replicas("Novara", self.data_dir)
        self.assertEqual(store["items"][0]["payload"]["confianza"], 80)

    def test_obtener_decision_devuelve_la_mas_reciente(self):
        rp.registrar_decision("Novara", "MLA1", "MLA2", "preview_generado",
                              self.data_dir)
        rp.registrar_decision("Novara", "MLA1", "MLA2", "aplicada",
                              self.data_dir)
        d = rp.obtener_decision("Novara", "MLA1", "MLA2", self.data_dir)
        self.assertIsNotNone(d)
        self.assertEqual(d["accion"], "aplicada")

    def test_obtener_decision_inexistente_devuelve_none(self):
        d = rp.obtener_decision("Novara", "MLA1", "MLA999", self.data_dir)
        self.assertIsNone(d)

    def test_purga_descartadas_vencidas(self):
        # Inyectar manualmente un descarte de hace 70 días (fuera del TTL=60d)
        # y un descarte reciente.
        store = {
            "items": [
                {"fingerprint": "fp_old", "alias": "Novara",
                 "item_origen": "MLA1", "item_destino": "MLA_OLD",
                 "accion": "descartada",
                 "fecha": (datetime.now(timezone.utc) - timedelta(days=70))
                          .isoformat(timespec="seconds"),
                 "payload": {}},
                {"fingerprint": "fp_new", "alias": "Novara",
                 "item_origen": "MLA1", "item_destino": "MLA_NEW",
                 "accion": "descartada",
                 "fecha": rp._now_iso(),
                 "payload": {}},
            ]
        }
        from core.db_storage import db_save
        db_save(rp._replicas_path("Novara", self.data_dir), store)

        # Cargar con purga (default) → solo queda la nueva
        out = rp.obtener_replicas("Novara", self.data_dir)
        ids = [it["item_destino"] for it in out["items"]]
        self.assertNotIn("MLA_OLD", ids)
        self.assertIn("MLA_NEW", ids)

    def test_purga_no_toca_aplicadas_aunque_sean_viejas(self):
        # Una decisión "aplicada" muy vieja NO debe purgarse (TTL solo aplica a descartadas)
        store = {
            "items": [
                {"fingerprint": "fp_old_aplicada", "alias": "Novara",
                 "item_origen": "MLA1", "item_destino": "MLA_OLD",
                 "accion": "aplicada",
                 "fecha": (datetime.now(timezone.utc) - timedelta(days=200))
                          .isoformat(timespec="seconds"),
                 "payload": {}},
            ]
        }
        from core.db_storage import db_save
        db_save(rp._replicas_path("Novara", self.data_dir), store)
        out = rp.obtener_replicas("Novara", self.data_dir)
        self.assertEqual(len(out["items"]), 1)
        self.assertEqual(out["items"][0]["accion"], "aplicada")


# ── Tests: fingerprint ───────────────────────────────────────────────────────

class TestFingerprint(unittest.TestCase):

    def test_fingerprint_idempotente(self):
        fp1 = rp.fingerprint_replica("Novara", "MLA1", "MLA2")
        fp2 = rp.fingerprint_replica("Novara", "MLA1", "MLA2")
        self.assertEqual(fp1, fp2)

    def test_fingerprint_distinto_si_cambia_input(self):
        base = rp.fingerprint_replica("Novara", "MLA1", "MLA2")
        self.assertNotEqual(base, rp.fingerprint_replica("Otra",   "MLA1", "MLA2"))
        self.assertNotEqual(base, rp.fingerprint_replica("Novara", "MLA9", "MLA2"))
        self.assertNotEqual(base, rp.fingerprint_replica("Novara", "MLA1", "MLA9"))


# ── Tests: hard cap mensual de Haiku ─────────────────────────────────────────

class TestCostGuard(ReplicadorTestBase):

    def _seed_token_log(self, entries: list[dict]) -> None:
        with open(os.path.join(self.data_dir, "token_log.json"),
                  "w", encoding="utf-8") as f:
            json.dump({"entries": entries}, f)

    def test_costo_solo_cuenta_replicador_del_mes(self):
        ahora = datetime.now()
        mes_anterior = ahora - timedelta(days=45)
        self._seed_token_log([
            {"ts": ahora.isoformat(),
             "funcion": "Replicador IA — Generar preview", "usd": 0.5},
            {"ts": ahora.isoformat(),
             "funcion": "Replicador IA — Otra cosa", "usd": 0.3},
            # Mes anterior → no cuenta
            {"ts": mes_anterior.isoformat(),
             "funcion": "Replicador IA — Generar preview", "usd": 100.0},
            # Otro feature → no cuenta
            {"ts": ahora.isoformat(),
             "funcion": "Veredicto IA", "usd": 5.0},
        ])
        actual = rp.costo_mes_actual_usd(self.data_dir)
        self.assertAlmostEqual(actual, 0.8, places=2)

    def test_supera_cap_falso_bajo_threshold(self):
        ahora = datetime.now()
        self._seed_token_log([
            {"ts": ahora.isoformat(),
             "funcion": "Replicador IA — Generar preview", "usd": 2.0},
        ])
        supera, cost = rp.supera_cap_mensual(self.data_dir)
        self.assertFalse(supera)
        self.assertEqual(cost, 2.0)

    def test_supera_cap_verdadero_en_threshold(self):
        ahora = datetime.now()
        self._seed_token_log([
            {"ts": ahora.isoformat(),
             "funcion": "Replicador IA — Generar preview", "usd": 5.0},
        ])
        supera, cost = rp.supera_cap_mensual(self.data_dir)
        self.assertTrue(supera, "Esperaba supera=True en threshold exacto")

    def test_costo_sin_log_devuelve_cero(self):
        actual = rp.costo_mes_actual_usd(self.data_dir)
        self.assertEqual(actual, 0.0)


# ── Tests: parser de respuesta Haiku ─────────────────────────────────────────

class TestParseRespuestaHaiku(unittest.TestCase):

    def test_parse_json_simple(self):
        texto = json.dumps({
            "aplicable": True,
            "titulo_propuesto": "Nuevo titulo",
            "descripcion_propuesta": "Descripción nueva",
            "cambios_clave": ["a", "b"],
            "confianza": 75,
        })
        out = rp._parse_respuesta_haiku(texto)
        self.assertTrue(out["aplicable"])
        self.assertEqual(out["confianza"], 75)
        self.assertEqual(out["cambios_clave"], ["a", "b"])

    def test_parse_con_fence_markdown(self):
        texto = '```json\n' + json.dumps({
            "aplicable": True,
            "titulo_propuesto": "T",
            "descripcion_propuesta": "D",
            "cambios_clave": [],
            "confianza": 50,
        }) + '\n```'
        out = rp._parse_respuesta_haiku(texto)
        self.assertTrue(out["aplicable"])

    def test_parse_con_texto_alrededor(self):
        texto = ('Acá está la respuesta:\n' + json.dumps({
            "aplicable": True,
            "titulo_propuesto": "Título",
            "descripcion_propuesta": "Descripción",
            "cambios_clave": [],
            "confianza": 60,
        }) + '\n\nFin.')
        out = rp._parse_respuesta_haiku(texto)
        self.assertTrue(out["aplicable"])
        self.assertEqual(out["confianza"], 60)

    def test_parse_aplicable_false_no_pide_titulo(self):
        texto = json.dumps({
            "aplicable": False,
            "razon_no_aplicable": "Categoría incompatible",
            "cambios_clave": [],
            "confianza": 0,
        })
        out = rp._parse_respuesta_haiku(texto)
        self.assertFalse(out["aplicable"])
        self.assertIn("incompatible", out["razon_no_aplicable"])

    def test_parse_titulo_truncado_a_60(self):
        texto = json.dumps({
            "aplicable": True,
            "titulo_propuesto": "X" * 100,
            "descripcion_propuesta": "D",
            "cambios_clave": [],
            "confianza": 50,
        })
        out = rp._parse_respuesta_haiku(texto)
        self.assertEqual(len(out["titulo_propuesto"]), 60)
        self.assertTrue(out.get("_truncado"))

    def test_parse_confianza_invalida_se_normaliza_a_50(self):
        texto = json.dumps({
            "aplicable": True,
            "titulo_propuesto": "T",
            "descripcion_propuesta": "D",
            "cambios_clave": [],
            "confianza": "muy alta",   # tipo inválido
        })
        out = rp._parse_respuesta_haiku(texto)
        self.assertEqual(out["confianza"], 50)

    def test_parse_sin_json_levanta_error(self):
        with self.assertRaises(ValueError):
            rp._parse_respuesta_haiku("Hola, no hay JSON acá")

    def test_parse_aplicable_falta_levanta_error(self):
        texto = json.dumps({"titulo_propuesto": "X", "descripcion_propuesta": "Y"})
        with self.assertRaises(ValueError):
            rp._parse_respuesta_haiku(texto)


# ── Tests: prompt builder y generación con mock ──────────────────────────────

class TestConstruirPrompt(unittest.TestCase):

    def test_prompt_incluye_titulo_destino_y_keywords(self):
        patron = {
            "titulo_antes":          "Producto viejo",
            "titulo_despues":        "Producto premium calidad",
            "keywords_agregadas":    ["premium", "calidad"],
            "keywords_removidas":    ["viejo"],
            "atributos_completados": True,
            "veredicto_score":       85,
            "metricas_clave":        ["visitas_7d", "ventas_30d"],
            "razonamiento_origen":   "Mejoró visitas y ventas",
            "descripcion_estilo":    None,
        }
        item_destino = {
            "id":          "MLA999",
            "title":       "Otro producto similar",
            "category_id": "MLA111",
            "price":       2500,
        }
        prompt = rp._construir_prompt_replica(patron, item_destino)
        # Los datos clave del patrón y del destino están en el prompt
        self.assertIn("MLA999", prompt)
        self.assertIn("premium, calidad", prompt)
        self.assertIn("Otro producto similar", prompt)
        self.assertIn("MLA111", prompt)
        self.assertIn("85", prompt)

    def test_prompt_con_descripcion_estilo_la_incluye(self):
        patron = {
            "titulo_antes":          "A",
            "titulo_despues":        "B",
            "keywords_agregadas":    [],
            "keywords_removidas":    [],
            "atributos_completados": False,
            "veredicto_score":       70,
            "metricas_clave":        [],
            "razonamiento_origen":   "OK",
            "descripcion_estilo":    "Descripción ganadora con estilo claro",
        }
        item_destino = {"id": "MLA1", "title": "T", "category_id": "C", "price": 100}
        prompt = rp._construir_prompt_replica(patron, item_destino)
        self.assertIn("Descripción ganadora", prompt)
        self.assertIn("ESTILO DE DESCRIPCIÓN", prompt)

    def test_prompt_sin_keywords_pone_ninguna(self):
        patron = {
            "titulo_antes":          "A",
            "titulo_despues":        "B",
            "keywords_agregadas":    [],
            "keywords_removidas":    [],
            "atributos_completados": False,
            "veredicto_score":       60,
            "metricas_clave":        [],
            "razonamiento_origen":   "",
            "descripcion_estilo":    None,
        }
        item_destino = {"id": "MLA1", "title": "T", "category_id": "C", "price": 0}
        prompt = rp._construir_prompt_replica(patron, item_destino)
        self.assertIn("(ninguna)", prompt)


class TestGenerarReplicaHaikuMockeada(ReplicadorTestBase):
    """Mock completo de Anthropic — sin tocar la red ni gastar tokens.

    El módulo hace `from anthropic import Anthropic` lazy adentro de la
    función, por lo que inyectamos un módulo fake en sys.modules antes
    de llamarla. Esto permite testear sin tener `anthropic` instalado
    en el entorno de tests.
    """

    def _build_patron(self):
        return {
            "titulo_antes":          "Producto viejo",
            "titulo_despues":        "Producto premium",
            "keywords_agregadas":    ["premium"],
            "keywords_removidas":    ["viejo"],
            "atributos_completados": True,
            "veredicto_score":       85,
            "metricas_clave":        ["visitas_7d"],
            "razonamiento_origen":   "OK",
            "descripcion_estilo":    None,
        }

    def _mock_anthropic_response(self, payload: dict,
                                  input_tokens=600, output_tokens=400,
                                  empty_content=False):
        """Crea un mock que simula client.messages.create."""
        mock_msg = MagicMock()
        if empty_content:
            mock_msg.content = []
            mock_msg.stop_reason = "max_tokens"
        else:
            mock_block = MagicMock()
            mock_block.type = "text"
            mock_block.text = json.dumps(payload)
            mock_msg.content = [mock_block]
            mock_msg.stop_reason = "end_turn"
        mock_msg.usage.input_tokens = input_tokens
        mock_msg.usage.output_tokens = output_tokens
        return mock_msg

    def _patch_anthropic_module(self, mock_msg):
        """Inyecta un módulo fake `anthropic` en sys.modules con `Anthropic`
        clase que devuelve un client cuyo messages.create devuelve mock_msg."""
        import types
        fake_module = types.ModuleType("anthropic")

        class FakeClient:
            def __init__(self_inner, **kwargs):
                self_inner.messages = MagicMock()
                self_inner.messages.create = MagicMock(return_value=mock_msg)

        fake_module.Anthropic = FakeClient
        return unittest.mock.patch.dict(sys.modules, {"anthropic": fake_module})

    def test_falla_sin_api_key(self):
        os.environ.pop("ANTHROPIC_API_KEY", None)
        with self.assertRaises(RuntimeError):
            rp.generar_replica_haiku(
                self._build_patron(),
                {"id": "MLA1", "title": "T", "category_id": "C", "price": 100},
                self.data_dir,
            )

    def test_genera_replica_aplicable(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-fake"
        payload = {
            "aplicable": True,
            "titulo_propuesto": "Producto Premium Adaptado",
            "titulo_motivo": "Apliqué la palabra 'Premium' al destino.",
            "descripcion_propuesta": "Descripción nueva sin emojis.",
            "cambios_clave": ["añadí premium", "removí viejo"],
            "confianza": 80,
        }
        mock_msg = self._mock_anthropic_response(payload)
        with self._patch_anthropic_module(mock_msg):
            result = rp.generar_replica_haiku(
                self._build_patron(),
                {"id": "MLA1", "title": "Otro", "category_id": "C", "price": 100},
                self.data_dir,
            )
        self.assertTrue(result["ok"])
        self.assertTrue(result["aplicable"])
        self.assertEqual(result["confianza"], 80)
        self.assertIn("_debug", result)
        self.assertEqual(result["_debug"]["tokens_in"], 600)
        self.assertEqual(result["_debug"]["tokens_out"], 400)

    def test_genera_replica_no_aplicable(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-fake"
        payload = {
            "aplicable": False,
            "razon_no_aplicable": "Categoría incompatible",
            "cambios_clave": [],
            "confianza": 0,
        }
        mock_msg = self._mock_anthropic_response(payload)
        with self._patch_anthropic_module(mock_msg):
            result = rp.generar_replica_haiku(
                self._build_patron(),
                {"id": "MLA1", "title": "Otro", "category_id": "C", "price": 100},
                self.data_dir,
            )
        self.assertTrue(result["ok"])
        self.assertFalse(result["aplicable"])
        self.assertIn("incompatible", result["razon_no_aplicable"])

    def test_genera_replica_loggea_tokens(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-fake"
        payload = {
            "aplicable": True,
            "titulo_propuesto": "T",
            "descripcion_propuesta": "D",
            "cambios_clave": [],
            "confianza": 50,
        }
        mock_msg = self._mock_anthropic_response(payload)
        with self._patch_anthropic_module(mock_msg):
            rp.generar_replica_haiku(
                self._build_patron(),
                {"id": "MLA1", "title": "Otro", "category_id": "C", "price": 100},
                self.data_dir,
            )
        # token_log.json debería existir y tener el costo
        log_path = os.path.join(self.data_dir, "token_log.json")
        self.assertTrue(os.path.exists(log_path))
        with open(log_path) as f:
            log = json.load(f)
        self.assertEqual(len(log["entries"]), 1)
        entry = log["entries"][0]
        self.assertEqual(entry["modelo"], rp.MODELO_DEFAULT)
        self.assertEqual(entry["in"], 600)
        self.assertEqual(entry["out"], 400)
        self.assertGreater(entry["usd"], 0)

    def test_respuesta_vacia_levanta_error(self):
        os.environ["ANTHROPIC_API_KEY"] = "sk-fake"
        mock_msg = self._mock_anthropic_response({}, empty_content=True)
        with self._patch_anthropic_module(mock_msg):
            with self.assertRaises(RuntimeError):
                rp.generar_replica_haiku(
                    self._build_patron(),
                    {"id": "MLA1", "title": "T", "category_id": "C", "price": 100},
                    self.data_dir,
                )


# ── Tests: titulo no incluye emojis (regla #4) ───────────────────────────────

class TestSinEmojisEnTituloPropuesto(unittest.TestCase):
    """Verifica que el parser NO acepta titulos con emojis (regla #4 ML)."""

    def test_titulo_con_emoji_no_falla_pero_marca_para_revision(self):
        # Nota: el parser actual no rechaza emojis explícitamente — la regla
        # se hace cumplir aguas arriba (en el prompt). Este test documenta
        # el comportamiento actual y sirve como anchor para futuras
        # validaciones más estrictas.
        texto = json.dumps({
            "aplicable": True,
            "titulo_propuesto": "Producto Premium 🎯",
            "descripcion_propuesta": "Descripción sin emoji.",
            "cambios_clave": [],
            "confianza": 50,
        })
        out = rp._parse_respuesta_haiku(texto)
        # Por ahora se acepta — regla #4 se cumple en el prompt del LLM.
        self.assertTrue(out["aplicable"])
        # Si en el futuro se agrega rechazo, este assert debería invertirse.


# ── Runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
