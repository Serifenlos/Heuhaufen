/*
 * Heuhaufen – kleine Helfer im Browser. Alles funktioniert auch ohne JavaScript.
 *
 * Das ist die QUELLE. CodeKit verkleinert sie nach build/app/static/js/app-min.js.
 * Tailwind durchsucht diese Datei ebenfalls nach Klassen und Attributen (siehe app.css).
 */
(function () {
  "use strict";

  // Bis November 2026 hieß das Tool „Ebay Watcher“. Was der Browser sich unter dem alten Namen gemerkt hat
  // (Seitengröße, Sortierung), einmal unter dem neuen Namen übernehmen. Die Ansicht „Karten/Liste“ der
  // Übersicht gibt es seit Issue #14 nicht mehr; ihr gemerkter Wert fliegt raus.
  try {
    ["ebay-watcher-view", "heuhaufen-view"].forEach(function (key) { localStorage.removeItem(key); });
    ["page-size", "sort"].forEach(function (name) {
      var old = localStorage.getItem("ebay-watcher-" + name);
      if (old === null) return;
      if (localStorage.getItem("heuhaufen-" + name) === null) localStorage.setItem("heuhaufen-" + name, old);
      localStorage.removeItem("ebay-watcher-" + name);
    });
  } catch (error) {
    // Speicher gesperrt (privates Surfen): dann gelten die Vorgaben
    console.info("Heuhaufen: gemerkte Einstellungen nicht übernommen (" + error.name + ")");
  }

  // ------------------------------------------------------------------
  // Restlaufzeiten aktuell halten (ui.countdown_span), gleiche Regeln wie util.countdown:
  // „8h54“, ab einem Tag „1 Tag 3h20“ bzw. „2 Tagen 3h20“, unter data-seconds (Erinnerung des Gesuchs) „0h09:42“.
  // In der letzten Stunde bekommt die Zeit data-soon, das die Klassen data-soon:… hervorheben.

  function pad(n) {
    return (n < 10 ? "0" : "") + n;
  }

  function format(left, secondsBelow) {
    var days = Math.floor(left / 86400);
    var rest = left % 86400;
    var hours = Math.floor(rest / 3600);
    var minutes = Math.floor((rest % 3600) / 60);
    if (days >= 1) return days + (days === 1 ? " Tag " : " Tagen ") + hours + "h" + pad(minutes);
    if (left < secondsBelow) return hours + "h" + pad(minutes) + ":" + pad(rest % 60);
    return hours + "h" + pad(minutes);
  }

  function tick() {
    var now = Date.now() / 1000;
    document.querySelectorAll("[data-end]").forEach(function (el) {
      var left = Math.floor(Number(el.dataset.end) - now);
      var time = el.querySelector("[data-left]");
      if (left <= 0) {
        if (time) el.textContent = el.hasAttribute("data-cap") ? "Beendet" : "beendet";
        return;
      }
      if (!time) return;
      var text = format(left, Number(el.dataset.seconds) || 600);
      if (time.textContent !== text) {
        var resized = time.textContent.length !== text.length;
        time.textContent = text;
        var line = resized ? time.closest("[data-fit]") : null;
        if (line) fitLine(line);
      }
      time.toggleAttribute("data-soon", left < 3600);
    });
  }

  // Wird eine Zeile mit „endet in …“ zu knapp (sie müsste umbrechen oder würde abgeschnitten), entfällt
  // „endet“: „in 2 Tagen 7h13“ (ui.endet_in, die Zeile trägt data-fit). Gemessen wird, sobald die Zeile zu
  // sehen ist und wenn sich ihre Breite ändert (ResizeObserver), außerdem wenn die Zeit länger oder kürzer wird.
  function shorten(line, short) {
    line.querySelectorAll("[data-endet]").forEach(function (word) { word.hidden = short; });
    line.querySelectorAll("[data-cap-in]").forEach(function (word) { word.textContent = short ? "In" : "in"; });
  }

  function fitLine(line) {
    if (!line.querySelector("[data-endet]")) return;
    shorten(line, false);
    var room = line.clientWidth;
    if (!room) return;   // gerade nicht zu sehen (andere Seite, zugeklappt)
    var wrap = line.style.whiteSpace;
    line.style.whiteSpace = "nowrap";   // so lang wäre die Zeile ohne Umbruch
    var tight = line.scrollWidth > room + 1;
    line.style.whiteSpace = wrap;
    if (tight) shorten(line, true);
  }

  // Beobachtet werden die Zeile und ihr Elternelement: Ist die Zeile nur so breit wie ihr Text, ändert sich
  // ihre eigene Größe nicht, wenn rundherum mehr Platz entsteht.
  var fitObserver = window.ResizeObserver ? new window.ResizeObserver(function (entries) {
    window.requestAnimationFrame(function () {   // erst im nächsten Bild ändern, sonst meldet der Browser eine Schleife
      entries.forEach(function (entry) {
        var box = entry.target;
        if (box.hasAttribute("data-fit")) fitLine(box);
        else box.querySelectorAll(":scope > [data-fit]").forEach(fitLine);
      });
    });
  }) : null;

  function watchFits(root) {
    root.querySelectorAll("[data-fit]").forEach(function (line) {
      if (!fitObserver) {
        fitLine(line);
        return;
      }
      fitObserver.observe(line);
      if (line.parentElement) fitObserver.observe(line.parentElement);
    });
  }

  if (!fitObserver) {
    window.addEventListener("resize", function () {
      document.querySelectorAll("[data-fit]").forEach(fitLine);
    });
  }

  tick();
  watchFits(document);
  setInterval(tick, 1000);   // jede Sekunde, damit kurz vor Ende die Sekunden laufen

  // ------------------------------------------------------------------
  // Seiten: lange Listen zeigen nur einen Teil der Angebote (Vorgabe 20, Feld „pro Seite“, auf diesem
  // Gerät gemerkt). Ausgeblendete Zeilen laden ihre Bilder erst, wenn ihre Seite drankommt.

  var PAGE_KEY = "heuhaufen-page-size";
  var DEFAULT_PAGE_SIZE = 20;

  function savedPageSize() {
    try {
      var n = parseInt(localStorage.getItem(PAGE_KEY), 10);
      return n >= 1 && n <= 200 ? n : DEFAULT_PAGE_SIZE;
    } catch (error) {
      console.info("Heuhaufen: Seitengröße nicht lesbar (" + error.name + ")");
      return DEFAULT_PAGE_SIZE;
    }
  }

  function rememberPageSize(n) {
    try {
      localStorage.setItem(PAGE_KEY, String(n));
    } catch (error) {
      console.info("Heuhaufen: Seitengröße wird nicht gespeichert (" + error.name + ")");
    }
  }

  var pageSize = savedPageSize();

  function pagerOf(list) {
    var next = list.nextElementSibling;
    return next && next.hasAttribute("data-pager") ? next : null;
  }

  // Seitenzahlen: erste, letzte und die beiden Nachbarn der aktuellen, dazwischen „…“
  function pageNumbers(page, pages) {
    var out = [];
    for (var p = 1; p <= pages; p++) {
      if (p === 1 || p === pages || Math.abs(p - page) <= 1) {
        out.push(p);
      } else if (out[out.length - 1] !== 0) {
        out.push(0);
      }
    }
    return out;
  }

  function pageButton(pager, text, label, target, current) {
    var button = pager.querySelector("template[data-page-button]").content.firstElementChild.cloneNode(true);
    button.textContent = text;
    button.setAttribute("aria-label", label);
    if (current) button.setAttribute("aria-current", "page");
    if (target === null) {
      button.disabled = true;
    } else {
      button.dataset.page = String(target);
    }
    return button;
  }

  function paginate(list, page) {
    var pager = pagerOf(list);
    if (!pager) return;
    var rows = list.querySelectorAll(":scope > li[data-offer]");
    var pages = Math.max(1, Math.ceil(rows.length / pageSize));
    var current = Math.min(Math.max(1, page || Number(list.dataset.page) || 1), pages);
    list.dataset.page = String(current);
    rows.forEach(function (row, i) {
      row.hidden = i < (current - 1) * pageSize || i >= current * pageSize;
    });

    pager.hidden = rows.length <= pageSize;
    pager.querySelector("[data-page-size]").value = String(pageSize);
    var first = (current - 1) * pageSize + 1;
    pager.querySelector("[data-range]").textContent =
      first + "–" + Math.min(current * pageSize, rows.length) + " von " + rows.length;

    var box = pager.querySelector("[data-pages]");
    box.textContent = "";
    box.appendChild(pageButton(pager, "‹", "Vorige Seite", current > 1 ? current - 1 : null, false));
    pageNumbers(current, pages).forEach(function (p) {
      if (p === 0) {
        box.appendChild(pager.querySelector("template[data-page-gap]").content.firstElementChild.cloneNode(true));
      } else {
        box.appendChild(pageButton(pager, String(p), "Seite " + p, p, p === current));
      }
    });
    box.appendChild(pageButton(pager, "›", "Nächste Seite", current < pages ? current + 1 : null, false));
  }

  function bindPagers(root) {
    root.querySelectorAll("ol[data-sortable]").forEach(function (list) {
      var pager = pagerOf(list);
      if (!pager) return;
      pager.querySelector("[data-pages]").addEventListener("click", function (event) {
        var button = event.target.closest("button[data-page]");
        if (!button) return;
        paginate(list, Number(button.dataset.page));
        if (list.getBoundingClientRect().top < 0) list.scrollIntoView();
      });
      pager.querySelector("[data-page-size]").addEventListener("change", function (event) {
        var n = parseInt(event.target.value, 10);
        if (!(n >= 1)) n = DEFAULT_PAGE_SIZE;
        pageSize = Math.min(200, n);
        rememberPageSize(pageSize);
        document.querySelectorAll("ol[data-sortable]").forEach(function (other) { paginate(other, 1); });
      });
      paginate(list, 1);
    });
  }

  bindPagers(document);

  // ------------------------------------------------------------------
  // Detailseite: Angebote sortieren (wird auf diesem Gerät gemerkt).
  // Jede Zeile trägt data-sort-end, data-sort-price, data-sort-new und data-sort-distance.

  var SORT_KEY = "heuhaufen-sort";
  var sortControl = document.querySelector("[data-sort-control]");
  var sortButtons = document.querySelectorAll("button[data-sort]");

  function rememberSort(key) {
    try {
      localStorage.setItem(SORT_KEY, key);
    } catch (error) {
      console.info("Heuhaufen: Sortierung wird nicht gespeichert (" + error.name + ")");
    }
  }

  function savedSort() {
    try {
      return localStorage.getItem(SORT_KEY);
    } catch (error) {
      console.info("Heuhaufen: gespeicherte Sortierung nicht lesbar (" + error.name + ")");
      return null;
    }
  }

  // Leere Werte (z. B. keine Endzeit) kommen immer ans Ende
  function sortValue(el, name, descending) {
    var raw = el.getAttribute("data-sort-" + name);
    if (raw === null || raw === "") return Infinity;
    var value = Number(raw);
    return descending ? -value : value;
  }

  var SORTS = {
    end: [["end", false], ["price", false]],
    price: [["price", false], ["end", false]],
    "new": [["new", true], ["end", false]],
    distance: [["distance", false], ["end", false]]
  };

  function compareBy(rules) {
    return function (a, b) {
      for (var i = 0; i < rules.length; i++) {
        var x = sortValue(a, rules[i][0], rules[i][1]);
        var y = sortValue(b, rules[i][0], rules[i][1]);
        if (x !== y) return x < y ? -1 : 1;
      }
      return 0;
    };
  }

  function currentSort() {
    var pressed = document.querySelector('button[data-sort][aria-pressed="true"]');
    return pressed ? pressed.dataset.sort : "end";
  }

  function applySort(key, root) {
    var rules = SORTS[key] || SORTS.end;
    (root || document).querySelectorAll("ol[data-sortable]").forEach(function (list) {
      var rows = Array.prototype.slice.call(list.querySelectorAll(":scope > li[data-offer]"));
      rows.sort(compareBy(rules)).forEach(function (row) { list.appendChild(row); });
      paginate(list, 1);
    });
  }

  function setSort(key) {
    sortButtons.forEach(function (b) { b.setAttribute("aria-pressed", String(b.dataset.sort === key)); });
    applySort(key);
    rememberSort(key);
  }

  if (sortControl) {
    sortControl.hidden = false;
    var wanted = savedSort();
    var available = Array.prototype.some.call(sortButtons, function (b) { return b.dataset.sort === wanted; });
    if (wanted && wanted !== "end" && available) setSort(wanted);
    sortButtons.forEach(function (b) {
      b.addEventListener("click", function () { setSort(b.dataset.sort); });
    });
  }

  // ------------------------------------------------------------------
  // Eingeklappte Listen („Mit Versand über dem Limit“, „Ausgeblendet“) erst beim Aufklappen laden

  function loadFold(fold) {
    if (fold.hasAttribute("data-loaded")) return;
    var target = fold.querySelector("[data-lazy-target]");
    fold.setAttribute("data-loaded", "");
    fetch(fold.dataset.lazy, { credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.text();
      })
      .then(function (html) {
        target.innerHTML = html;
        bindPagers(target);
        applySort(currentSort(), target);
        bindHideForms(target);
        tick();
        watchFits(target);
      })
      .catch(function (error) {
        fold.removeAttribute("data-loaded");
        console.info("Heuhaufen: Liste konnte nicht geladen werden (" + error.message + ")");
      });
  }

  document.querySelectorAll("details[data-lazy]").forEach(function (fold) {
    fold.addEventListener("toggle", function () { if (fold.open) loadFold(fold); });
  });

  // ------------------------------------------------------------------
  // Ausblenden ohne Neuladen der Seite: Zeile entfernen und Zähler anpassen, danach einige Sekunden
  // „Rückgängig“ anbieten (search_detail.html, [data-undo]). Ausblenden geht auch in der Galerie.
  // Ohne JavaScript schickt das Formular ganz normal ab.

  var UNDO_SECONDS = 10;
  var undoTimer = null;

  function changeCount(name, delta) {
    var el = document.querySelector('[data-count="' + name + '"]');
    if (el) el.textContent = String(Math.max(0, Number(el.textContent) + delta));
  }

  // Liste „Ausgeblendet“: Zähler anpassen, ohne Einträge verstecken, beim nächsten Aufklappen neu laden
  function hiddenChanged(delta) {
    var fold = document.getElementById("ausgeblendet");
    if (!fold) return;
    changeCount("hidden", delta);
    var count = fold.querySelector('[data-count="hidden"]');
    fold.hidden = !count || Number(count.textContent) === 0;
    fold.removeAttribute("data-loaded");
    if (fold.open && !fold.hidden) loadFold(fold);
  }

  function post(url, form) {
    return fetch(url, { method: "POST", body: new FormData(form), credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
      });
  }

  function offerUndo(form, row, ol, next, list) {
    var box = document.querySelector("[data-undo]");
    if (!box) return;
    window.clearTimeout(undoTimer);
    box.hidden = false;
    box.querySelector("[data-undo-button]").onclick = function () {
      window.clearTimeout(undoTimer);
      box.hidden = true;
      post(form.action.replace(/\/hide$/, "/unhide"), form)
        .then(function () {
          // Zeile an ihren alten Platz zurück und deren Seite zeigen
          ol.insertBefore(row, next && next.parentNode === ol ? next : null);
          var rows = Array.prototype.slice.call(ol.querySelectorAll(":scope > li[data-offer]"));
          paginate(ol, Math.floor(rows.indexOf(row) / pageSize) + 1);
          if (list) changeCount(list.dataset.list, 1);
          hiddenChanged(-1);
          tick();
        })
        .catch(function () { window.location.reload(); });
    };
    undoTimer = window.setTimeout(function () { box.hidden = true; }, UNDO_SECONDS * 1000);
  }

  function hideOffer(form) {
    var row = form.closest("li");
    var ol = row.parentElement;
    var next = row.nextElementSibling;
    var list = row.closest("[data-list]");
    post(form.action, form)
      .then(function () {
        row.remove();
        paginate(ol);
        if (list) changeCount(list.dataset.list, -1);
        hiddenChanged(1);
        offerUndo(form, row, ol, next, list);
      })
      .catch(function () { form.submit(); });
  }

  function bindHideForms(root) {
    root.querySelectorAll("form[data-hide]").forEach(function (form) {
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        hideOffer(form);
      });
    });
  }

  bindHideForms(document);

  // ------------------------------------------------------------------
  // Bilder-Galerie: Klick aufs Vorschaubild oder den Titel zeigt alle Bilder des Angebots (PhotoSwipe,
  // photoswipe.com). /galerie/… liefert Adressen und Maße der Bilder aus dem Bild-Cache. PhotoSwipe selbst
  // (js/photoswipe.js, gebündelt aus node_modules) lädt erst beim ersten Klick, die Adresse steht in
  // <body data-photoswipe>. Ohne JavaScript oder mit Befehls-/Strg-Taste führen Bild und Titel zur Plattform
  // (eBay …, Name in data-platform). In der Leiste oben: Ausblenden (wie das Auge in der Liste), Angaben
  // ein/aus, bei der Plattform öffnen.
  // Seitenleiste (includes/gallery_panel.html, kommt fertig mit /galerie/…): beim Öffnen eingeblendet,
  // der Knopf ⓘ blendet sie aus und ein. Ab 64rem Breite steht sie rechts, darunter unten als Schublade.

  var GALLERY_OPTIONS = {
    bgOpacity: 1,
    showHideAnimationType: "fade",
    wheelToZoom: true,
    tapAction: false,          // Tippen aufs Bild am Handy: nichts tun (Doppeltippen zoomt weiter)
    preload: [1, 2],
    closeTitle: "Schließen (Esc)",
    zoomTitle: "Vergrößern (Z)",
    arrowPrevTitle: "Vorheriges Bild",
    arrowNextTitle: "Nächstes Bild",
    errorMsg: "Dieses Bild lässt sich gerade nicht laden.",
    indexIndicatorSep: " / "
  };
  // Knopf „Bei eBay öffnen“ (bzw. der Plattform) oben in der Galerie (Pfeil aus dem Kasten, 32er-Raster wie die PhotoSwipe-Symbole)
  var EBAY_ICON = '<path id="pswp__icn-ebay" d="M9 11h6v2h-4v8h8v-4h2v6H9zM17 9h6v6h-2v-2.6l-5.3 5.3-1.4-1.4 5.3-5.3H17z"/>';
  var HIDE_ICON = '<g id="pswp__icn-hide"><path fill-rule="evenodd" d="M5.5 16c2.4-4.2 6-6.3 10.5-6.3s8.1 2.1 10.5 6.3c-2.4 4.2-6 6.3-10.5 6.3S7.9 20.2 5.5 16zm2.4 0c2 3 4.7 4.4 8.1 4.4s6.1-1.4 8.1-4.4c-2-3-4.7-4.4-8.1-4.4S9.9 13 7.9 16z"/><circle cx="16" cy="16" r="2.6"/><path d="M8.2 6.8l17 17-1.4 1.4-17-17z"/></g>';
  var INFO_ICON = '<path id="pswp__icn-info" fill-rule="evenodd" d="M16 6.5a9.5 9.5 0 1 1 0 19 9.5 9.5 0 0 1 0-19zm0 2a7.5 7.5 0 1 0 0 15 7.5 7.5 0 0 0 0-15zM15 14h2v7h-2zm0-3.5h2v2h-2z"/>';
  var PANEL_WIDTH = 380;                                   // w-95 in gallery_panel.html
  var wideGallery = window.matchMedia("(min-width: 64rem)");   // lg: Leiste rechts statt unten
  var photoSwipe = null;

  function loadPhotoSwipe() {
    if (!photoSwipe) {
      photoSwipe = new window.Promise(function (resolve, reject) {
        var script = document.createElement("script");
        script.src = document.body.dataset.photoswipe;
        script.onload = function () {
          if (window.PhotoSwipe) resolve(window.PhotoSwipe);
          else reject(new Error("PhotoSwipe fehlt"));
        };
        script.onerror = function () {
          photoSwipe = null;
          reject(new Error("PhotoSwipe nicht geladen"));
        };
        document.head.appendChild(script);
      });
    }
    return photoSwipe;
  }

  // Antworten von /galerie/… eine Minute aufheben: Beim Blättern (unten) lädt das Nachbarangebot schon,
  // während man wischt, und wer zurückblättert, muss nicht noch einmal warten.
  var GALLERY_CACHE = 60000;
  var galleryCache = {};

  function loadImages(link) {
    var url = link.dataset.gallery;
    var hit = galleryCache[url];
    if (hit && Date.now() - hit.at < GALLERY_CACHE) return hit.promise;
    var promise = fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      });
    galleryCache[url] = { at: Date.now(), promise: promise };
    promise.catch(function () { delete galleryCache[url]; });
    return promise;
  }

  // Vorab laden, samt erstem Bild
  function warmGallery(link) {
    if (!link) return;
    loadImages(link)
      .then(function (data) {
        if (!data.items || !data.items[0]) return;
        var image = new window.Image();
        image.src = data.items[0].src;
      })
      .catch(function () { /* beim Öffnen wird es noch einmal versucht */ });
  }

  // Am Handy ist die Seitenleiste eine Schublade am unteren Rand (includes/gallery_panel.html). s.top ist ihre
  // Oberkante: zwischen peekTop (nur der Kopf schaut heraus) und minTop (Galerie auf Mindesthöhe).
  var SHEET_MIN = 0.3;   // Galerie am Handy mindestens 30 % der Bildschirmhöhe

  function sheetBounds(pswp, s) {
    var height = pswp.element.clientHeight;
    var minTop = Math.round(height * SHEET_MIN);
    return { height: height, minTop: minTop, peekTop: Math.max(minTop, height - Math.min(s.peekHeight, height * 0.6)) };
  }

  // Platz fürs Bild: neben der Seitenleiste bzw. über der Schublade, damit sie nichts verdeckt
  function galleryPadding(pswp, s) {
    if (!s.panel || s.panel.hidden) return { top: 0, bottom: 0, left: 0, right: 0 };
    if (wideGallery.matches) return { top: 60, bottom: 16, left: 16, right: PANEL_WIDTH + 16 };
    return { top: 56, bottom: pswp.element.clientHeight - s.top + 8, left: 0, right: 0 };
  }

  // row: Zeile der Liste, aus der die Galerie kam (zum Blättern, addFlip). carry: kommt man durch Blättern
  // hierher (openGallery), steht darin, wie weit die Schublade offen war.
  function addPanel(pswp, html, carry, row) {
    var s = { panel: null, body: null, peek: null, info: null, top: 0, peekHeight: 0, frame: 0, anim: 0, drag: null,
              zoomHidden: false, zoomLevel: null, flip: null };
    pswp.options.paddingFn = function () { return galleryPadding(pswp, s); };

    // Bild an die Schublade anpassen, höchstens einmal je Bildschirmaufbau
    function followSheet() {
      if (s.frame) return;
      s.frame = window.requestAnimationFrame(function () {
        s.frame = 0;
        pswp.updateSize(true);
      });
    }

    function setTop(top) {
      var b = sheetBounds(pswp, s);
      s.top = Math.min(b.peekTop, Math.max(b.minTop, top));
      s.panel.style.height = (b.height - s.top) + "px";
      followSheet();
    }

    function animateTo(target) {
      window.cancelAnimationFrame(s.anim);
      var from = s.top;
      var start = null;
      function step(time) {
        if (start === null) start = time;
        var p = Math.min(1, (time - start) / 260);
        setTop(from + (target - from) * (1 - Math.pow(1 - p, 3)));
        if (p < 1) s.anim = window.requestAnimationFrame(step);
      }
      s.anim = window.requestAnimationFrame(step);
    }

    // Größe neu bestimmen (Öffnen, Drehen, Fenstergröße, Ein- und Ausblenden): am Rechner Leiste rechts, am
    // Handy Schublade. Die Schublade bleibt, wo sie war; nur beim ersten Mal schaut nur der Kopf heraus,
    // außer man hat hierher geblättert: dann so weit offen wie beim vorigen Angebot.
    function relayout() {
      if (!s.panel) return;
      if (wideGallery.matches) {
        s.panel.style.height = "";
      } else if (!s.panel.hidden) {
        var first = !s.peekHeight;
        s.peekHeight = s.peek.getBoundingClientRect().bottom - s.panel.getBoundingClientRect().top + s.body.scrollTop + 20;
        var b = sheetBounds(pswp, s);
        var top = s.top;
        if (first) top = !carry || carry.sheet === "peek" ? b.peekTop : carry.sheet === "open" ? b.minTop : carry.top;
        s.top = Math.min(b.peekTop, Math.max(b.minTop, top));
        s.panel.style.height = (b.height - s.top) + "px";
      }
      pswp.updateSize(true);
    }

    // Wie weit die Schublade offen ist, fürs nächste Angebot beim Blättern
    s.sheetState = function () {
      var b = sheetBounds(pswp, s);
      return { sheet: s.top >= b.peekTop - 1 ? "peek" : s.top <= b.minTop + 1 ? "open" : "", top: s.top };
    };

    function showPanel(show) {
      s.panel.hidden = !show;
      s.info.setAttribute("aria-pressed", String(show));
      pswp.element.dataset.panel = show ? "open" : "closed";
      relayout();
    }

    // Lupe (Knopf oder Taste Z): Zum Vergrößern verschwindet die Leiste, das Bild bekommt den ganzen Bildschirm.
    // Beim Verkleinern kommt sie so zurück, wie sie war. Ebenso, wenn man anders zurückzoomt (Doppeltippen,
    // Finger, Mausrad) oder zum nächsten Bild blättert.
    var toggleZoom = pswp.toggleZoom;
    pswp.toggleZoom = function () {
      var slide = pswp.currSlide;
      if (slide && s.panel) {
        var zoomedIn = slide.currZoomLevel !== slide.zoomLevels.initial;
        if (!zoomedIn && !s.panel.hidden) {
          s.zoomHidden = true;
          showPanel(false);
        } else if (zoomedIn && s.zoomHidden) {
          s.zoomHidden = false;
          showPanel(true);
        }
      }
      toggleZoom.call(pswp);
    };

    function restoreAfterZoom() {
      if (!s.zoomHidden) return;
      s.zoomHidden = false;
      window.requestAnimationFrame(function () { showPanel(true); });   // nicht mitten in PhotoSwipes Animation
    }

    pswp.on("zoomPanUpdate", function (event) {
      var level = event.slide.currZoomLevel;
      var initial = event.slide.zoomLevels.initial;
      if (s.zoomLevel !== null && s.zoomLevel > initial + 0.001 && level <= initial + 0.001) restoreAfterZoom();
      s.zoomLevel = level;
    });
    pswp.on("change", function () {
      s.zoomLevel = null;
      restoreAfterZoom();
    });

    // Wischen auf der Schublade: erst die Schublade schieben, ist sie oben, den Text scrollen (das macht der
    // Browser). Zurück nach unten geht es, sobald der Text wieder am Anfang steht. Seitlich gewischt (auch
    // in der Leiste am Rechner, z. B. auf dem iPad) blättert zum nächsten oder vorigen Angebot (s.flip).
    function onTouchStart(event) {
      if (event.touches.length !== 1 || (wideGallery.matches && !s.flip)) { s.drag = null; return; }
      window.cancelAnimationFrame(s.anim);
      var t = event.touches[0];
      s.drag = { x0: t.clientX, y0: t.clientY, top0: s.top, scroll0: s.body.scrollTop, mode: "",
                 samples: [{ x: t.clientX, y: t.clientY, t: event.timeStamp }] };
    }

    function onTouchMove(event) {
      var d = s.drag;
      if (!d) return;
      var x = event.touches[0].clientX;
      var y = event.touches[0].clientY;
      var b = sheetBounds(pswp, s);
      if (!d.mode) {
        var dx = Math.abs(x - d.x0);
        var dy = Math.abs(y - d.y0);
        if (s.flip && dx > dy) {
          if (dx < 6) return;   // erst ein paar Pixel abwarten, ob es wirklich seitwärts geht
          d.mode = "offer";
        } else if (wideGallery.matches) {
          if (dy) s.drag = null;   // Leiste rechts: den Text scrollt der Browser
          return;
        } else {
          // gleich bei der ersten Bewegung entscheiden, sonst fängt Safari schon an, den Text zu scrollen
          if (y === d.y0) return;
          var open = s.top <= b.minTop + 1;
          d.mode = !open || (y > d.y0 && s.body.scrollTop <= 0) ? "sheet" : "text";
        }
      }
      d.samples.push({ x: x, y: y, t: event.timeStamp });
      if (d.samples.length > 5) d.samples.shift();
      if (d.mode === "offer") {
        event.preventDefault();
        s.flip.follow(x - d.x0);
        return;
      }
      if (d.mode !== "sheet") return;
      event.preventDefault();
      var want = d.top0 + (y - d.y0);
      if (want < b.minTop) s.body.scrollTop = d.scroll0 + (b.minTop - want);   // oben angekommen: Text weiter
      setTop(want);
    }

    function onTouchEnd() {
      var d = s.drag;
      s.drag = null;
      if (!d || d.samples.length < 2) return;
      // Schwung: kräftig nach oben oder unten gewischt, dann ganz auf bzw. zu
      var first = d.samples[0];
      var last = d.samples[d.samples.length - 1];
      var dt = Math.max(1, last.t - first.t);
      if (d.mode === "offer") {
        s.flip.release(last.x - d.x0, (last.x - first.x) / dt);
        return;
      }
      if (d.mode !== "sheet") return;
      var speed = (last.y - first.y) / dt;
      var b = sheetBounds(pswp, s);
      if (speed < -0.4 && s.top > b.minTop) animateTo(b.minTop);
      else if (speed > 0.4 && s.top < b.peekTop) animateTo(b.peekTop);
    }

    // Mausrad (schmales Fenster am Rechner): wie Wischen. Nie an PhotoSwipe weitergeben, sonst zoomt das Bild.
    // Seitlich (Trackpad) blättert zum nächsten oder vorigen Angebot.
    function onWheel(event) {
      event.stopPropagation();
      if (s.flip && Math.abs(event.deltaX) > Math.abs(event.deltaY)) {
        event.preventDefault();   // sonst blättert der Browser im Verlauf zurück
        s.flip.wheel(event.deltaX);
        return;
      }
      if (wideGallery.matches) return;
      var b = sheetBounds(pswp, s);
      if ((event.deltaY > 0 && s.top > b.minTop) || (event.deltaY < 0 && s.body.scrollTop <= 0 && s.top < b.peekTop)) {
        event.preventDefault();
        setTop(s.top - event.deltaY);
      }
    }

    pswp.on("uiRegister", function () {
      pswp.ui.registerElement({
        name: "info",
        order: 8,
        isButton: true,
        title: "Angaben ein- oder ausblenden",
        ariaLabel: "Angaben zum Angebot ein- oder ausblenden",
        html: { isCustomSVG: true, inner: INFO_ICON, outlineID: "pswp__icn-info" },
        onInit: function (el) {
          s.info = el;
          el.setAttribute("aria-pressed", "true");
        },
        onClick: function () {
          s.zoomHidden = false;   // von Hand ein- oder ausgeblendet: so bleibt es
          showPanel(s.panel.hidden);
        }
      });
      pswp.ui.registerElement({
        name: "panel",
        appendTo: "root",
        onInit: function (el) {
          el.innerHTML = html;
          s.panel = el.querySelector("[data-gallery-panel]");
          s.body = el.querySelector("[data-sheet-body]");
          s.peek = el.querySelector("[data-sheet-peek]");
          pswp.element.dataset.panel = "open";
          s.panel.addEventListener("touchstart", onTouchStart, { passive: true });
          s.panel.addEventListener("touchmove", onTouchMove, { passive: false });
          s.panel.addEventListener("touchend", onTouchEnd);
          s.panel.addEventListener("touchcancel", function () {
            if (s.drag && s.drag.mode === "offer") {   // abgebrochen: nicht blättern
              s.drag = null;
              s.flip.release(0, 0);
            } else {
              onTouchEnd();
            }
          });
          s.panel.addEventListener("wheel", onWheel, { passive: false });
          // Griff: ganz hochschieben bzw. zurück zur großen Galerie
          el.querySelector("[data-sheet-handle]").addEventListener("click", function () {
            var b = sheetBounds(pswp, s);
            animateTo(s.top > (b.minTop + b.peekTop) / 2 ? b.minTop : b.peekTop);
          });
          if (row) s.flip = addFlip(pswp, s, row, carry);
          if (el.isConnected) relayout();
          tick();
          watchFits(el);
        }
      });
    });
    // Nach dem Einblenden noch einmal messen: während der Öffnen-Animation passt PhotoSwipe die Bildgröße nicht an
    pswp.on("afterInit", function () { window.requestAnimationFrame(relayout); });
    pswp.on("openingAnimationEnd", relayout);
    wideGallery.addEventListener("change", relayout);
    window.addEventListener("resize", relayout);
    pswp.on("destroy", function () {
      window.cancelAnimationFrame(s.anim);
      wideGallery.removeEventListener("change", relayout);
      window.removeEventListener("resize", relayout);
      if (fitObserver && s.panel) {
        s.panel.querySelectorAll("[data-fit]").forEach(function (line) {
          fitObserver.unobserve(line);
          fitObserver.unobserve(line.parentElement);
        });
      }
    });
    return s;
  }

  function platformName(link) {
    return link.getAttribute("data-platform") || "eBay";
  }

  // ------------------------------------------------------------------
  // Blättern zwischen Angeboten (Issue #10): In der Schublade bzw. Seitenleiste nach links wischen zeigt das
  // nächste, nach rechts das vorige Angebot der Liste, aus der die Galerie kam, in deren Reihenfolge
  // (Sortierung, über alle Seiten). Am Rechner auch mit den Pfeilen oben in der Leiste oder seitlich mit dem
  // Trackpad. Das neue Angebot kommt als eigene Galerie über die alte, die danach verschwindet; die
  // Schublade bleibt so weit offen, wie sie war. Nach dem Schließen steht die Liste beim zuletzt gezeigten.

  var FLIP_AT = 0.22;       // so weit (Anteil der Breite) muss man wischen, außer mit Schwung
  var FLIP_SPEED = 0.35;    // Schwung in Pixel je Millisekunde
  var FLIP_WHEEL = 60;      // Trackpad: so weit seitlich, dann blättern
  var flipWheel = { sum: 0, done: false, timer: 0 };   // gilt über die Galerien hinweg (Nachschwung des Trackpads)

  function offerRows(row) {
    var list = row.parentElement;
    return list ? Array.prototype.slice.call(list.querySelectorAll(":scope > li[data-offer]")) : [];
  }

  // Zeile in der Liste zeigen: wenn nötig auf ihre Seite wechseln, dann hinscrollen (beim Schließen der
  // Galerie, während sie ausblendet). Den Fokus bekommt die Zeile erst, wenn die Galerie weg ist.
  function revealRow(pswp, row) {
    var list = row.parentElement;
    if (!list || !row.isConnected) return;
    if (row.hidden) paginate(list, Math.floor(offerRows(row).indexOf(row) / pageSize) + 1);
    var box = row.getBoundingClientRect();
    if (box.top < 0 || box.bottom > window.innerHeight) row.scrollIntoView({ block: "center" });
    pswp.on("destroy", function () {
      var link = row.querySelector("a[data-gallery]");
      if (link) link.focus({ preventScroll: true });
    });
  }

  function addFlip(pswp, s, row, carry) {
    var rows = offerRows(row);
    var index = rows.indexOf(row);
    var nav = s.panel.querySelector("[data-offer-nav]");
    if (!nav || index < 0 || rows.length < 2) return null;
    var pos = nav.querySelector("[data-offer-pos]");
    var busy = false;
    var closed = false;
    var warm = {};

    function target(step) {
      var next = rows[index + step];
      return next && next.isConnected ? next.querySelector("a[data-gallery]") : null;
    }

    function label(i) {
      pos.textContent = "Angebot " + (i + 1) + " von " + rows.length;
    }

    function width() {
      return s.panel.clientWidth || 1;
    }

    function slide(frames, options) {
      return s.body.animate ? s.body.animate(frames, options) : null;
    }

    // Inhalt folgt dem Finger; am Anfang oder Ende der Liste nur ein Stück, mit Widerstand
    function follow(dx) {
      if (busy) return;
      var step = dx < 0 ? 1 : -1;
      if (!target(step)) {
        dx = dx / 4;
      } else if (!warm[step]) {
        warm[step] = true;
        warmGallery(target(step));   // lädt schon, während man wischt
      }
      s.body.style.transform = dx ? "translateX(" + dx + "px)" : "";
      s.body.style.opacity = dx ? String(1 - Math.min(0.5, Math.abs(dx) / width())) : "";
    }

    function settle() {   // zurück an den Platz
      var from = s.body.style.transform;
      var opacity = s.body.style.opacity || "1";
      s.body.style.transform = "";
      s.body.style.opacity = "";
      if (from) slide([{ transform: from, opacity: opacity }, { transform: "translateX(0)", opacity: 1 }], { duration: 180, easing: "ease-out" });
    }

    function go(step) {
      var link = target(step);
      if (busy || closed || !link) return;
      busy = true;
      label(index + step);
      var out = slide([{ transform: s.body.style.transform || "translateX(0)", opacity: s.body.style.opacity || "1" },
                       { transform: "translateX(" + (-step * width()) + "px)", opacity: 0 }],
                      { duration: 160, easing: "ease-in", fill: "forwards" });
      var state = s.sheetState();
      openGallery(link, {
        old: pswp,
        step: step,
        sheet: state.sheet,
        top: state.top,
        cancelled: function () { return closed; },
        fail: function () {
          busy = false;
          label(index);
          if (out) out.cancel();
          settle();
        }
      });
    }

    function release(dx, speed) {
      if (busy) return;
      var step = dx < 0 ? 1 : -1;
      var far = Math.abs(dx) > width() * FLIP_AT || (Math.abs(dx) > 24 && speed * dx > 0 && Math.abs(speed) > FLIP_SPEED);
      if (far && target(step)) go(step);
      else settle();
    }

    function wheel(deltaX) {
      window.clearTimeout(flipWheel.timer);
      flipWheel.timer = window.setTimeout(function () {
        flipWheel.sum = 0;
        flipWheel.done = false;
      }, 250);
      if (flipWheel.done) return;
      flipWheel.sum += deltaX;
      if (Math.abs(flipWheel.sum) < FLIP_WHEEL) return;
      flipWheel.done = true;   // eine Geste, ein Angebot
      go(flipWheel.sum > 0 ? 1 : -1);
    }

    nav.hidden = false;
    label(index);
    nav.querySelectorAll("[data-offer-step]").forEach(function (button) {
      var step = Number(button.dataset.offerStep);
      button.disabled = !target(step);
      button.addEventListener("click", function () { go(step); });
    });
    pswp.on("close", function () { closed = true; });
    if (carry) {
      // hereinschieben, von der Seite, zu der man gewischt hat; das nächste in der Richtung schon laden
      slide([{ transform: "translateX(" + (carry.step * width() * 0.3) + "px)", opacity: 0 },
             { transform: "translateX(0)", opacity: 1 }], { duration: 200, easing: "ease-out" });
      warm[carry.step] = true;
      warmGallery(target(carry.step));
    }
    return { follow: follow, release: release, wheel: wheel };
  }

  // Vorschaubild und Titel einer Zeile öffnen dieselbe Galerie (includes/offer.html). carry: beim Blättern
  // (addFlip) die bisherige Galerie und wie weit die Schublade offen war; die neue erscheint dann ohne
  // Einblenden darüber, und klappt es nicht, bleibt man beim bisherigen Angebot.
  function openGallery(link, carry) {
    var row = link.closest("li[data-offer]");
    var links = row ? Array.prototype.slice.call(row.querySelectorAll("a[data-gallery]")) : [link];
    var thumb = row ? row.querySelector("a[data-gallery] img") : link.querySelector("img");
    var hideForm = row ? row.querySelector("form[data-hide]") : null;
    links.forEach(function (a) { a.setAttribute("aria-busy", "true"); });
    window.Promise.all([loadPhotoSwipe(), loadImages(link)])
      .then(function (results) {
        var PhotoSwipe = results[0];
        var items = results[1].items || [];
        if (carry && carry.cancelled()) return;   // inzwischen geschlossen
        if (!items.length && !results[1].panel) throw new Error("keine Bilder");
        if (!items.length) {   // keine Bilder, aber Beschreibung: Galerie trotzdem öffnen
          items = [{ html: '<p class="grid h-full place-items-center px-6 text-center text-ui text-white/70">Zu diesem Angebot liefert ' + platformName(link) + ' gerade keine Bilder.</p>' }];
        } else if (thumb) {
          items[0].msrc = thumb.currentSrc || thumb.src;   // Vorschaubild, bis das große Bild da ist
        }
        var options = Object.assign({ dataSource: items }, GALLERY_OPTIONS);
        if (carry) {
          options.showAnimationType = "none";
          options.returnFocus = false;   // macht revealRow
        }
        var pswp = new PhotoSwipe(options);
        if (results[1].panel) addPanel(pswp, results[1].panel, carry, row);
        pswp.on("uiRegister", function () {
          if (hideForm) {
            // Ausblenden: Galerie schließen, Zeile entfernen, unten „Rückgängig“ anbieten
            pswp.ui.registerElement({
              name: "hide",
              order: 7,
              isButton: true,
              title: "Ausblenden",
              ariaLabel: "Angebot ausblenden",
              html: { isCustomSVG: true, inner: HIDE_ICON, outlineID: "pswp__icn-hide" },
              onClick: function () {
                pswp.close();
                hideOffer(hideForm);
              }
            });
          }
          pswp.ui.registerElement({
            name: "ebay",
            order: 9,
            isButton: true,
            tagName: "a",
            title: "Bei " + platformName(link) + " öffnen",
            ariaLabel: "Bei " + platformName(link) + " öffnen",
            html: { isCustomSVG: true, inner: EBAY_ICON, outlineID: "pswp__icn-ebay" },
            onInit: function (el) {
              el.href = link.href;
              el.target = "_blank";
              el.rel = "noopener noreferrer";
            }
          });
        });
        if (carry) {
          // Die alte Galerie gibt Tastatur und Fokus ab und verschwindet, sobald die neue steht
          var old = carry.old;
          var dropped = false;
          var drop = function () {
            if (dropped) return;
            dropped = true;
            old.destroy();
          };
          old.options.returnFocus = false;
          old.events.removeAll();
          pswp.on("openingAnimationEnd", drop);
          window.setTimeout(drop, 500);
          if (row) pswp.on("close", function () { revealRow(pswp, row); });
        }
        pswp.init();
      })
      .catch(function (error) {
        if (carry) {   // beim Blättern: beim bisherigen Angebot bleiben
          console.info("Heuhaufen: Angebot nicht geladen (" + error.message + ")");
          carry.fail();
          return;
        }
        // Ohne Galerie wenigstens das Angebot bei der Plattform zeigen, wie ohne JavaScript
        console.info("Heuhaufen: Galerie nicht verfügbar (" + error.message + ")");
        var tab = window.open(link.href, "_blank");
        if (tab) tab.opener = null;
        else window.location.assign(link.href);   // Pop-up gesperrt: dann eben in diesem Tab
      })
      .then(function () { links.forEach(function (a) { a.removeAttribute("aria-busy"); }); });
  }

  document.addEventListener("click", function (event) {
    var link = event.target.closest ? event.target.closest("a[data-gallery]") : null;
    if (!link || !document.body.dataset.photoswipe) return;
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;   // neuer Tab: Plattform
    event.preventDefault();
    if (link.getAttribute("aria-busy") !== "true") openGallery(link);
  });

  // ------------------------------------------------------------------
  // Sicherheitsabfrage vor dem Löschen

  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.dataset.confirm)) e.preventDefault();
    });
  });

  // ------------------------------------------------------------------
  // Gesuch-Formular: Erinnerung nur zeigen, wenn Auktionen dabei sind

  var formatInputs = document.querySelectorAll('input[name="format"]');

  function syncFormat() {
    var checked = document.querySelector('input[name="format"]:checked');
    var value = checked ? checked.value : "auction";
    document.querySelectorAll("[data-when-format]").forEach(function (el) {
      el.hidden = el.dataset.whenFormat.split(" ").indexOf(value) === -1;
    });
  }

  formatInputs.forEach(function (i) { i.addEventListener("change", syncFormat); });
  if (formatInputs.length) syncFormat();

  // ------------------------------------------------------------------
  // Ziehen zum Neuladen (Issue #11). In der Web-App vom Home-Bildschirm (iOS: „Als Web-App öffnen“)
  // gibt es das nicht von selbst, deshalb nur dort: ganz oben nach unten ziehen, nach PULL_DEAD + PULL_AT
  // loslassen. Nie in Dialogen, in der Galerie oder in Bereichen, die selbst scrollen (Kategorienliste).
  // Dazu: Wer nach AWAY_RELOAD zur App zurückkehrt, bekommt die Seite frisch, außer bei offenen
  // Eingaben, offenem Dialog oder offener Galerie. Neu geladen wird data-reload (main.reload_url), nie ein Formular.

  var PULL_DEAD = 24;                  // die ersten px Fingerweg: noch nichts (versehentliches Ziehen)
  var PULL_AT = 140;                   // danach so weit ziehen, zusammen gut 160 px (etwa 2,5–3 cm)
  var AWAY_RELOAD = 5 * 60 * 1000;     // ms
  var pull = document.querySelector("[data-pull]");
  var standalone = window.navigator.standalone === true ||
    (typeof window.matchMedia === "function" && window.matchMedia("(display-mode: standalone)").matches);
  var formTouched = false;

  function reloadPage() {
    window.location.replace(document.body.dataset.reload || window.location.pathname);
  }

  function galleryOpen() {
    return !!document.querySelector(".pswp--open");
  }

  // Offener Dialog (Kategorien) oder offenes Info-Fenster (ui.info_panel, Popover). Browser ohne Popover
  // kennen den Selektor :popover-open nicht, deshalb erst nachsehen.
  var canPopover = typeof document.body.showPopover === "function";

  function dialogOpen() {
    return !!document.querySelector("dialog[open]") || (canPopover && !!document.querySelector(":popover-open"));
  }

  // Liegt der Finger in einem Bereich, der selbst nach oben/unten scrollt?
  function inScrollArea(el) {
    for (; el && el.nodeType === 1 && el !== document.body && el !== document.documentElement; el = el.parentElement) {
      var overflow = window.getComputedStyle(el).overflowY;
      if ((overflow === "auto" || overflow === "scroll") && el.scrollHeight > el.clientHeight + 1) return true;
    }
    return false;
  }

  function noPull(target) {
    return galleryOpen() || dialogOpen() || inScrollArea(target) ||
      !!(target.closest && target.closest(".pswp, dialog, input, textarea, select, [data-aspect-suggest]"));
  }

  if (pull && standalone) {
    var mark = pull.querySelector("[data-pull-mark]");
    var startX = 0;
    var startY = 0;
    var distance = 0;
    var tracking = false;
    var busy = false;

    var showPull = function (d) {
      var p = Math.min(d / PULL_AT, 1);
      pull.hidden = false;
      pull.style.opacity = String(p);
      pull.style.transform = "translateY(" + Math.round(Math.min(d * 0.5, PULL_AT * 0.6)) + "px)";
      mark.style.transform = "rotate(" + Math.round(p * 300) + "deg)";
    };

    var hidePull = function () {
      pull.removeAttribute("data-dragging");
      pull.style.opacity = "0";
      pull.style.transform = "";
      window.setTimeout(function () { if (!busy && !tracking) pull.hidden = true; }, 250);
    };

    document.addEventListener("touchstart", function (e) {
      tracking = !busy && e.touches.length === 1 && window.scrollY <= 0 && !noPull(e.target);
      if (!tracking) return;
      startX = e.touches[0].clientX;
      startY = e.touches[0].clientY;
      distance = 0;
    }, { passive: true });

    document.addEventListener("touchmove", function (e) {
      if (!tracking) return;
      var dx = e.touches[0].clientX - startX;
      var dy = e.touches[0].clientY - startY;
      // nach oben gescrollt, seitlich gewischt oder ein Dialog ging auf: kein Neuladen
      if (window.scrollY > 0 || dy < -8 || dialogOpen() || (dy < PULL_DEAD && Math.abs(dx) > 12 && Math.abs(dx) > dy)) {
        tracking = false;
        if (distance > 0) hidePull();
        return;
      }
      distance = Math.max(0, dy - PULL_DEAD);
      if (!distance && pull.hidden) return;
      pull.setAttribute("data-dragging", "");
      showPull(distance);
    }, { passive: true });

    var endPull = function () {
      if (!tracking) return;
      tracking = false;
      if (distance >= PULL_AT) {
        busy = true;
        pull.removeAttribute("data-dragging");
        pull.setAttribute("data-loading", "");
        mark.style.transform = "";
        reloadPage();
      } else if (distance > 0) {
        hidePull();
      }
    };
    document.addEventListener("touchend", endPull);
    document.addEventListener("touchcancel", function () {
      distance = 0;
      endPull();
    });
  }

  if (standalone) {
    var hiddenAt = 0;
    var touched = function () { formTouched = true; };   // Eingaben, Häkchen, Kategorien …
    document.addEventListener("input", touched);
    document.addEventListener("change", touched);
    document.addEventListener("click", function (e) {   // z. B. Kategorie-Chip entfernt
      if (e.target.closest && e.target.closest("form button[type=button]")) touched();
    });
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "hidden") {
        hiddenAt = Date.now();
      } else if (hiddenAt && Date.now() - hiddenAt >= AWAY_RELOAD && !formTouched && !galleryOpen() && !dialogOpen()) {
        reloadPage();
      }
    });
  }

  // ------------------------------------------------------------------
  // Kategorien im Gesuch-Formular (Issue #13): Dialog zum Durchklicken wie in der eBay-App, dazu Suche
  // und Vorschläge zum Suchbegriff. Die Listen kommen als HTML von /kategorien/… (category_list.html).
  // Gewählte Kategorien stehen als Chips mit verstecktem Feld „category“ im Formular.

  var catBox = document.querySelector("[data-categories]");
  var catDialog = document.querySelector("[data-cat-dialog]");

  if (catBox && catDialog && typeof catDialog.showModal === "function") {
    var catBase = "/kategorien/" + encodeURIComponent(catBox.dataset.platform);
    var catMax = Number(catBox.dataset.max) || 10;
    var catChosen = catBox.querySelector("[data-cat-chosen]");
    var catTemplate = catBox.querySelector("[data-cat-chip-template]");
    var catBody = catDialog.querySelector("[data-cat-body]");
    var catSuggest = catDialog.querySelector("[data-cat-suggest]");
    var catTitle = catDialog.querySelector("[data-cat-title]");
    var catBack = catDialog.querySelector("[data-cat-back]");
    var catSearch = catDialog.querySelector("[data-cat-search]");
    var catCount = catDialog.querySelector("[data-cat-count]");
    var catQuery = document.getElementById("query");
    var catStarted = false;
    var catCurrent = "";      // angezeigte Ebene (leer: oberste)
    var catUp = "";           // eine Ebene höher
    var catSuggested = null;  // Suchbegriff der geladenen Vorschläge
    var catTimer = null;
    var catTicket = 0;

    var catKeys = function () {
      return Array.prototype.map.call(catChosen.querySelectorAll("input[name=category]"), function (i) {
        return i.value;
      });
    };

    var catSync = function () {
      var keys = catKeys();
      catDialog.querySelectorAll("[data-cat-pick]").forEach(function (box) {
        box.checked = keys.indexOf(box.value) !== -1;
        box.disabled = !box.checked && keys.length >= catMax;
      });
      catCount.textContent = keys.length
        ? keys.length + " von höchstens " + catMax + " gewählt"
        : "Keine gewählt: Heuhaufen sucht in allen Kategorien.";
    };

    var catAdd = function (box) {
      var keys = catKeys();
      if (keys.indexOf(box.value) !== -1 || keys.length >= catMax) return;
      var chip = catTemplate.content.firstElementChild.cloneNode(true);
      var name = chip.querySelector("[data-cat-chip-name]");
      chip.querySelector("input").value = box.value;
      name.textContent = box.dataset.name;
      name.title = box.dataset.path ? box.dataset.path + " › " + box.dataset.name : box.dataset.name;
      chip.querySelector("[data-cat-remove]").setAttribute("aria-label", box.dataset.name + " entfernen");
      catChosen.appendChild(chip);
    };

    var catRemove = function (key) {
      catChosen.querySelectorAll("input[name=category]").forEach(function (input) {
        if (input.value === key) input.closest("[data-cat-chip]").remove();
      });
    };

    // HTML-Stück laden; bei der Hauptliste zählt nur die zuletzt angefragte (schnelles Tippen, Klicken)
    var catLoad = function (url, target, done) {
      var ticket = target === catBody ? ++catTicket : 0;
      fetch(url, { credentials: "same-origin" })
        .then(function (response) {
          if (!response.ok) throw new Error("HTTP " + response.status);
          return response.text();
        })
        .then(function (html) {
          if (ticket && ticket !== catTicket) return;
          target.innerHTML = html;
          catSync();
          if (done) done(target.querySelector("[data-cat-list]"));
        })
        .catch(function (error) {
          console.info("Kategorien:", error);
          if (target === catBody) target.innerHTML = '<p class="py-6 text-over">Die Kategorien ließen sich nicht laden.</p>';
        });
    };

    var catShow = function (parent) {
      catCurrent = parent || "";
      catLoad(catBase + (catCurrent ? "?parent=" + encodeURIComponent(catCurrent) : ""), catBody, function (list) {
        if (!list) return;
        catTitle.textContent = list.dataset.title;
        catUp = list.dataset.up;
        catBack.hidden = list.dataset.level !== "sub";
        catSuggest.hidden = list.dataset.level === "sub";
        catBody.parentNode.scrollTop = 0;
      });
    };

    var catSuggestions = function () {
      var q = catQuery ? catQuery.value.trim() : "";
      if (q === catSuggested) return;
      catSuggested = q;
      if (q.length < 2) {
        catSuggest.innerHTML = "";
        return;
      }
      catLoad(catBase + "/vorschlaege?q=" + encodeURIComponent(q), catSuggest);
    };

    catBox.querySelector("[data-cat-dialog-open]").hidden = false;
    catBox.querySelector("[data-cat-dialog-open]").addEventListener("click", function () {
      catDialog.showModal();
      if (!catStarted) {
        catStarted = true;
        catShow("");
      }
      catSuggestions();
      catSync();
    });
    catDialog.querySelector("[data-cat-done]").addEventListener("click", function () { catDialog.close(); });
    catDialog.addEventListener("click", function (e) {
      if (e.target === catDialog) {   // Klick neben den Dialog
        catDialog.close();
        return;
      }
      var open = e.target.closest("[data-cat-open]");
      if (open) catShow(open.dataset.catOpen);
    });
    catDialog.addEventListener("change", function (e) {
      var box = e.target.closest("[data-cat-pick]");
      if (!box) return;
      if (box.checked) catAdd(box); else catRemove(box.value);
      catSync();
    });
    catBack.addEventListener("click", function () { catShow(catUp); });
    catSearch.addEventListener("input", function () {
      window.clearTimeout(catTimer);
      catTimer = window.setTimeout(function () {
        var text = catSearch.value.trim();
        if (text.length < 2) {
          catShow(catCurrent);
          return;
        }
        catLoad(catBase + "/suche?q=" + encodeURIComponent(text), catBody, function () {
          catTitle.textContent = "Suche";
          catBack.hidden = true;
          catSuggest.hidden = true;
        });
      }, 250);
    });
    catChosen.addEventListener("click", function (e) {
      var remove = e.target.closest("[data-cat-remove]");
      if (!remove) return;
      remove.closest("[data-cat-chip]").remove();
      catSync();
    });
  }

  // ------------------------------------------------------------------
  // Artikelmerkmale im Gesuch-Formular (Issue #12): Paare aus Merkmal und Wert. Ist das letzte Paar
  // ausgefüllt, kommt ein leeres dazu. Ohne Kategorie ein Hinweis: eBay filtert nur in Kategorien.
  // Vorschläge aus den gewählten Kategorien über /merkmale/…, geladen beim ersten Klick in ein Feld, in einer
  // eigenen Liste unter dem Feld (Issue #16, statt <datalist>): mit der Zahl der Angebote bei eBay, beim Tippen
  // gefiltert, oben ein Schalter Häufigkeit ⇄ A–Z (auf diesem Gerät gemerkt). Pfeiltasten, Enter, Esc.
  // Am Handy steht die Liste über dem Feld, wenn darunter wegen der Tastatur kein Platz ist.

  var aspBox = document.querySelector("[data-aspects]");

  if (aspBox) {
    var aspRows = aspBox.querySelector("[data-aspect-rows]");
    var aspTemplate = aspBox.querySelector("[data-aspect-template]");
    var aspNeeds = aspBox.querySelector("[data-aspect-needs-category]");
    var aspError = aspBox.querySelector("#aspects-error");
    var aspChosen = document.querySelector("[data-cat-chosen]");
    var aspData = { names: [], values: {} };
    var aspLoaded = null;
    var aspPending = false;

    var sug = aspBox.querySelector("[data-aspect-suggest]");
    var sugList = sug.querySelector("[data-aspect-options]");
    var sugCount = sug.querySelector("[data-aspect-suggest-count]");
    var sugSort = sug.querySelector("[data-aspect-sort]");
    var sugSortLabel = sug.querySelector("[data-aspect-sort-label]");
    var sugOption = sug.querySelector("template[data-aspect-option]");
    var ASPECT_SORT_KEY = "heuhaufen-aspect-sort";
    var sugState = { input: null, items: [], shown: [], active: -1, filter: "", az: false };

    try {
      sugState.az = localStorage.getItem(ASPECT_SORT_KEY) === "az";
    } catch (error) {
      console.info("Heuhaufen: Sortierung der Vorschläge nicht lesbar (" + error.name + ")");
    }

    var aspInputs = function (row) {
      return row.querySelectorAll("input");
    };

    var aspCategories = function () {
      if (!aspChosen) return [];
      return Array.prototype.map.call(aspChosen.querySelectorAll("input[name=category]"), function (i) { return i.value; });
    };

    var aspCheck = function () {
      var rows = aspRows.querySelectorAll("[data-aspect-row]");
      var last = rows[rows.length - 1];
      if (!last || (aspInputs(last)[0].value.trim() && aspInputs(last)[1].value.trim())) {
        aspRows.appendChild(aspTemplate.content.firstElementChild.cloneNode(true));
      }
      var any = Array.prototype.some.call(aspRows.querySelectorAll("input"), function (i) { return i.value.trim(); });
      aspNeeds.hidden = !any || aspCategories().length > 0 || !!(aspError && !aspError.hidden);
      if (aspError && aspCategories().length) aspError.hidden = true;   // Kategorie nachgetragen
    };

    // Vorschläge für ein Feld: Merkmale, oder die Werte zum Merkmal derselben Zeile (Groß/klein egal)
    var sugItemsFor = function (input) {
      if (input.name === "aspect_name") return aspData.names || [];
      var name = input.closest("[data-aspect-row]").querySelector("[name=aspect_name]").value.trim().toLowerCase();
      var values = aspData.values || {};
      var match = Object.keys(values).filter(function (k) { return k.toLowerCase() === name; })[0];
      return match ? values[match] : [];
    };

    // Liste an die Zeile hängen: links bündig mit dem Feld, mindestens 240 px breit, nicht über die Zeile hinaus.
    // Unter dem Feld, außer dort ist (Tastatur am Handy) zu wenig Platz und darüber mehr.
    var sugPlace = function () {
      var input = sugState.input;
      if (!input || sug.hidden) return;
      var row = input.closest("[data-aspect-row]");
      var width = Math.min(row.clientWidth, Math.max(input.offsetWidth, 240));
      sug.style.width = width + "px";
      sug.style.left = Math.max(0, Math.min(input.offsetLeft, row.clientWidth - width)) + "px";
      var view = window.visualViewport;
      var viewTop = view ? view.offsetTop : 0;
      var viewBottom = view ? view.offsetTop + view.height : window.innerHeight;
      var box = input.getBoundingClientRect();
      var below = viewBottom - box.bottom - 12;
      var above = box.top - viewTop - 12;
      var up = below < 200 && above > below;
      sug.style.top = up ? "auto" : "";
      sug.style.bottom = up ? "100%" : "";
      sug.style.marginBottom = up ? "0.25rem" : "";
      sug.style.maxHeight = Math.max(140, Math.min(320, up ? above : below)) + "px";
    };

    var sugClose = function () {
      if (sugState.input) {
        sugState.input.setAttribute("aria-expanded", "false");
        sugState.input.removeAttribute("aria-activedescendant");
      }
      sugState.input = null;
      sug.hidden = true;
      aspBox.appendChild(sug);   // zurück an seinen Platz, damit es mit einer entfernten Zeile nicht verschwindet
    };

    var sugMark = function (index) {
      var options = sugList.children;
      if (!options.length) return;
      sugState.active = Math.max(0, Math.min(options.length - 1, index));
      Array.prototype.forEach.call(options, function (li, i) {
        li.setAttribute("aria-selected", String(i === sugState.active));
      });
      var current = options[sugState.active];
      sugState.input.setAttribute("aria-activedescendant", current.id);
      current.scrollIntoView({ block: "nearest" });
    };

    var sugRender = function () {
      var input = sugState.input;
      if (!input) return;
      var all = sugState.items.slice();
      if (sugState.az) {
        all.sort(function (a, b) { return a[0].localeCompare(b[0], "de", { numeric: true, sensitivity: "base" }); });
      }
      var needle = sugState.filter.trim().toLowerCase();
      var shown = needle ? all.filter(function (item) { return item[0].toLowerCase().indexOf(needle) !== -1; }) : all;
      sugState.shown = shown;
      sugState.active = -1;
      input.removeAttribute("aria-activedescendant");
      sugSortLabel.textContent = sugState.az ? "A–Z" : "Häufigkeit";
      sugSort.setAttribute("aria-pressed", String(sugState.az));
      var what = input.name === "aspect_name" ? "Merkmale" : "Werte";
      if (aspPending && !all.length) {
        sugCount.textContent = "Lade Vorschläge …";
      } else if (needle) {
        sugCount.textContent = shown.length + " von " + all.length + " " + what;
      } else {
        sugCount.textContent = all.length + " " + what;
      }
      sugList.textContent = "";
      shown.forEach(function (item, i) {
        var li = sugOption.content.firstElementChild.cloneNode(true);
        li.id = "aspect-option-" + i;
        li.dataset.value = item[0];
        li.querySelector("[data-text]").textContent = item[0];
        li.querySelector("[data-count]").textContent = Number(item[1]).toLocaleString("de-DE");
        sugList.appendChild(li);
      });
      var open = aspPending || shown.length > 0;
      sug.hidden = !open;
      input.setAttribute("aria-expanded", String(open));
      if (open) sugPlace();
    };

    var sugOpen = function (input) {
      if (!aspCategories().length) return;
      var row = input.closest("[data-aspect-row]");
      if (sug.parentNode !== row) row.appendChild(sug);
      sugState.input = input;
      sugState.items = sugItemsFor(input);
      sugState.filter = "";   // beim Hineinklicken alle zeigen, auch wenn schon etwas drinsteht
      sugRender();
    };

    var sugChoose = function (value) {
      var input = sugState.input;
      if (!input) return;
      input.value = value;
      input.dispatchEvent(new window.Event("input", { bubbles: true }));
      var row = input.closest("[data-aspect-row]");
      var valueInput = row.querySelector("[name=aspect_value]");
      if (input.name === "aspect_name" && !valueInput.value.trim()) {
        valueInput.focus();   // gleich weiter zum Wert, dessen Liste öffnet sich
      } else {
        sugClose();
      }
    };

    var aspLoad = function () {
      var cats = aspCategories();
      var query = document.getElementById("query");
      var q = query ? query.value.trim() : "";
      var key = cats.join(",") + "|" + q;
      if (!cats.length || key === aspLoaded) return;
      aspLoaded = key;
      aspPending = true;
      var params = "q=" + encodeURIComponent(q) + cats.map(function (c) { return "&c=" + encodeURIComponent(c); }).join("");
      fetch("/merkmale/" + encodeURIComponent(aspBox.dataset.platform) + "?" + params, { credentials: "same-origin" })
        .then(function (response) { return response.ok ? response.json() : { names: [], values: {} }; })
        .then(function (data) {
          aspData = data;
        })
        .catch(function (error) {
          console.info("Merkmale:", error);
          aspLoaded = null;
        })
        .then(function () {
          aspPending = false;
          if (sugState.input) {
            sugState.items = sugItemsFor(sugState.input);
            sugRender();
          }
        });
    };

    var isAspectInput = function (el) {
      return el && (el.name === "aspect_name" || el.name === "aspect_value");
    };

    aspRows.addEventListener("input", function (e) {
      aspCheck();
      if (e.target === sugState.input) {
        sugState.filter = e.target.value;
        sugRender();
      }
    });
    aspRows.addEventListener("focusin", function (e) {
      if (!isAspectInput(e.target)) return;
      aspLoad();
      sugOpen(e.target);
    });
    aspRows.addEventListener("focusout", function (e) {
      if (e.target === sugState.input && !sug.contains(e.relatedTarget)) sugClose();
    });
    aspRows.addEventListener("keydown", function (e) {
      if (!isAspectInput(e.target)) return;
      var open = sugState.input === e.target && !sug.hidden;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        if (!open) sugOpen(e.target);
        if (!sugList.children.length) return;
        e.preventDefault();
        var step = e.key === "ArrowDown" ? 1 : -1;
        sugMark(sugState.active < 0 ? (step > 0 ? 0 : sugList.children.length - 1) : sugState.active + step);
      } else if (e.key === "Enter" && open && sugState.active >= 0) {
        e.preventDefault();   // nicht das Formular abschicken
        sugChoose(sugState.shown[sugState.active][0]);
      } else if (e.key === "Escape" && open) {
        e.preventDefault();
        sugClose();
      }
    });
    // Tippen in die Liste: Fokus bleibt im Feld (Tastatur am Handy bleibt offen), Scrollen geht weiter
    sug.addEventListener("mousedown", function (e) { e.preventDefault(); });
    sug.addEventListener("click", function (e) {
      if (e.target.closest("[data-aspect-sort]")) {
        sugState.az = !sugState.az;
        try {
          localStorage.setItem(ASPECT_SORT_KEY, sugState.az ? "az" : "freq");
        } catch (error) {
          console.info("Heuhaufen: Sortierung der Vorschläge nicht gespeichert (" + error.name + ")");
        }
        sugRender();
        sugList.scrollTop = 0;
        return;
      }
      var option = e.target.closest("[role=option]");
      if (option) sugChoose(option.dataset.value);
    });
    if (window.visualViewport) {
      window.visualViewport.addEventListener("resize", sugPlace);
      window.visualViewport.addEventListener("scroll", sugPlace);
    }
    window.addEventListener("resize", sugPlace);

    aspRows.addEventListener("click", function (e) {
      var remove = e.target.closest("[data-aspect-remove]");
      if (!remove) return;
      sugClose();
      var row = remove.closest("[data-aspect-row]");
      if (aspRows.querySelectorAll("[data-aspect-row]").length > 1) {
        row.remove();
      } else {
        aspInputs(row).forEach(function (i) { i.value = ""; });
      }
      aspCheck();
    });
    if (aspChosen && typeof window.MutationObserver === "function") {
      new window.MutationObserver(aspCheck).observe(aspChosen, { childList: true });
    }
    aspCheck();
  }

  // ------------------------------------------------------------------
  // Nutzer: Text der Einladung kopieren (Knopf erscheint nur mit JavaScript)

  function copyText(field) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(field.value);
    }
    return new Promise(function (resolve, reject) {   // http im Heimnetz: Clipboard-API fehlt
      field.focus();
      field.select();
      if (document.execCommand("copy")) resolve(); else reject(new Error("copy"));
    });
  }

  document.querySelectorAll("[data-copy]").forEach(function (button) {
    var field = document.getElementById(button.dataset.copy);
    var done = button.parentNode.querySelector("[data-copy-done]");
    if (!field) return;
    button.hidden = false;
    button.addEventListener("click", function () {
      copyText(field)
        .then(function () {
          if (done) {
            done.hidden = false;
            window.setTimeout(function () { done.hidden = true; }, 2500);
          }
        })
        .catch(function (error) {
          console.info("Kopieren nicht möglich, Text ist markiert:", error);
          field.focus();
          field.select();
        });
    });
  });
})();
