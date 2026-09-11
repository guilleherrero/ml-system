/* Enviar a Cerebro — captura una pagina de resultados de MercadoLibre.
 *
 * ML bloqueo el acceso programatico a /sites/MLA/search, asi que el sistema ya
 * no puede ver quien aparece en una busqueda. Este script corre en el navegador
 * del usuario, con su sesion real, sobre la pagina que esta mirando: es la
 * unica via que queda para lo que no es catalogo, que es donde se compite por
 * contenido y no solo por precio.
 *
 * Recorre las TARJETAS de resultado, no los links sueltos. Probado contra la
 * pagina real: iterar anchors mezclaba el link del titulo con el de "otra
 * opcion de compra" y perdia la mitad de las filas.
 *
 * Dos cosas que parecen iguales y no lo son: una tarjeta de catalogo linkea a
 * /p/MLA123 —eso es una FICHA, no una publicacion— y una tradicional linkea a
 * la publicacion. Mandar el id de ficha como si fuera un competidor ensucia los
 * datos, asi que se marca cual es cual y el servidor resuelve las fichas.
 */
(function () {
  var CFG = window.__CEREBRO_CFG__ || {};
  var BASE = CFG.base, ALIAS = CFG.alias, TOKEN = CFG.token || '';

  function aviso(texto, color, ms) {
    var d = document.getElementById('cerebro-aviso') || document.createElement('div');
    d.id = 'cerebro-aviso';
    d.style.cssText = 'position:fixed;z-index:2147483647;top:16px;right:16px;' +
      'max-width:360px;padding:14px 16px;border-radius:10px;font:14px/1.55 system-ui,sans-serif;' +
      'color:#fff;box-shadow:0 8px 24px rgba(0,0,0,.25);background:' + (color || '#1e293b');
    d.textContent = texto;
    document.body.appendChild(d);
    if (ms) setTimeout(function () { if (d.parentNode) d.remove(); }, ms);
    return d;
  }

  if (!/mercadolibre\.com\.ar/.test(location.hostname)) {
    aviso('Esto se usa sobre una búsqueda de MercadoLibre. Buscá el producto como lo buscaría un comprador y volvé a hacer clic.', '#b45309', 7000);
    return;
  }

  function txt(el) { return (el && el.textContent || '').replace(/\s+/g, ' ').trim(); }

  function precio(card) {
    var f = card.querySelector('[class*="money-amount__fraction"]');
    if (!f) return 0;
    var n = parseFloat(txt(f).replace(/[^\d]/g, ''));
    return isNaN(n) ? 0 : n;
  }

  function leer() {
    var cards = document.querySelectorAll('li[class*="ui-search-layout__item"]');
    if (!cards.length) cards = document.querySelectorAll('[class*="poly-card--grid"], [class*="poly-card--list"]');
    var vistos = {}, filas = [];
    for (var i = 0; i < cards.length; i++) {
      var c = cards[i];
      var aT = c.querySelector('a[class*="poly-component__title"]') || c.querySelector('a[href]');
      if (!aT) continue;
      var hrefT = aT.getAttribute('href') || '';

      var mItem  = hrefT.match(/\/(MLA-?\d{9,})/);
      var aFicha = c.querySelector('a[href*="/p/MLA"]');
      var mFicha = (aFicha && (aFicha.getAttribute('href') || '').match(/\/p\/(MLA\d+)/))
                   || hrefT.match(/\/p\/(MLA\d+)/);

      var id   = mItem ? mItem[1].replace('-', '') : null;
      var cpid = mFicha ? mFicha[1] : null;
      var esFicha = !id;
      if (!id && cpid) id = cpid;            // solo se ve la ficha, no la publicacion
      if (!id || vistos[id]) continue;
      vistos[id] = true;

      var todo = txt(c);
      var vend = txt(c.querySelector('[class*="poly-component__seller"]'));
      var sold = todo.match(/([\d.]+)\s*vendidos?/i);
      var stock = todo.match(/[ÚU]ltim[ao]s?\s*\d*[^.|]{0,30}/i);
      var img = c.querySelector('img');

      filas.push({
        id: id,
        es_ficha: esFicha,
        catalog_product_id: cpid,
        title: txt(c.querySelector('[class*="poly-component__title"]')) || txt(aT),
        price: precio(c),
        permalink: hrefT.split('?')[0],
        thumbnail: img ? (img.getAttribute('data-src') || img.src || '') : '',
        seller: vend || '-',
        free_ship: /(env[íi]o|llega)\s+gratis/i.test(todo),
        sold_quantity: sold ? parseInt(sold[1].replace(/\./g, ''), 10) : null,
        senal_stock: stock ? stock[0].slice(0, 60) : '',
        posicion: filas.length + 1,
      });
    }
    return filas;
  }

  var cargando = aviso('Leyendo la página…');

  // La grilla de ML se arma despues del HTML: si se hace clic apenas carga,
  // todavia no hay nada que leer. Se reintenta un rato antes de rendirse.
  var intentos = 0;
  (function esperar() {
    var filas = leer();
    if (filas.length) return enviar(filas);
    if (++intentos > 12) {
      cargando.remove();
      aviso('No encontré publicaciones. Esperá a que termine de cargar la página de resultados y volvé a hacer clic.', '#b45309', 7000);
      return;
    }
    setTimeout(esperar, 700);
  })();

  function enviar(filas) {
    var q = '';
    try {
      q = new URLSearchParams(location.search).get('q') || '';
      if (!q) {
        var p = location.pathname.split('/').filter(Boolean);
        q = decodeURIComponent(p[p.length - 1] || '').replace(/-/g, ' ');
      }
    } catch (e) { q = ''; }

    cargando.textContent = 'Enviando ' + filas.length + ' publicaciones a Cerebro…';

    fetch(BASE + '/api/capturar-competidor', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Cerebro-Token': TOKEN},
      body: JSON.stringify({alias: ALIAS, query: q, token: TOKEN, competidores: filas}),
    })
      .then(function (r) { return r.json(); })
      .then(function (j) {
        cargando.remove();
        if (!j.ok) { aviso('Cerebro no lo aceptó: ' + (j.error || ''), '#b91c1c', 9000); return; }
        var msg = j.guardados + ' competidores capturados de "' + (j.query || 'esta búsqueda') + '"';
        if (j.fichas_resueltas) msg += ' (' + j.fichas_resueltas + ' desde fichas de catálogo)';
        msg += '. ';
        msg += (j.mis_posiciones && j.mis_posiciones.length)
          ? 'Aparecés en el puesto ' + j.mis_posiciones.map(function (p) { return p.posicion; }).join(' y ') + '.'
          : 'No apareciste en esta página.';
        msg += ' Confirmá cuáles son directos en Cerebro → Competidores.';
        aviso(msg, '#166534', 11000);
      })
      .catch(function (e) {
        cargando.remove();
        aviso('No pude llegar al sistema: ' + e, '#b91c1c', 9000);
      });
  }
})();
