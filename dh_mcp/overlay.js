// Injected into every page (context.add_init_script).
// Provides: a visible animated mouse cursor, click ripple, highlight box,
// and an interactive-element snapshot used by the AI to pick targets.
(() => {
  if (window.__dh) return;
  const ROOT_ID = "__dh_overlay_root";
  const state = { x: -100, y: -100, visible: false };

  function root() {
    let r = document.getElementById(ROOT_ID);
    if (r) return r;
    r = document.createElement("div");
    r.id = ROOT_ID;
    r.setAttribute("aria-hidden", "true");
    r.style.cssText =
      "position:fixed;inset:0;pointer-events:none;z-index:2147483647;overflow:visible;";
    r.innerHTML = `
      <div id="__dh_hl" style="position:fixed;display:none;border:3px solid #ff3b30;border-radius:8px;
           box-shadow:0 0 0 4px rgba(255,59,48,.25),0 0 18px rgba(255,59,48,.55);transition:all .25s ease;"></div>
      <div id="__dh_label" style="position:fixed;display:none;background:#ff3b30;color:#fff;font:600 14px/1.4 sans-serif;
           padding:2px 8px;border-radius:6px;white-space:nowrap;"></div>
      <div id="__dh_cursor" style="position:fixed;left:0;top:0;width:28px;height:28px;display:none;
           transform:translate(-100px,-100px);will-change:transform;filter:drop-shadow(0 2px 3px rgba(0,0,0,.45));">
        <svg width="28" height="28" viewBox="0 0 28 28"><path d="M3 2 L3 22 L8.5 17 L12.5 26 L16 24.5 L12 15.5 L19.5 15.5 Z"
          fill="#fff" stroke="#111" stroke-width="1.6" stroke-linejoin="round"/></svg>
      </div>`;
    (document.documentElement || document.body).appendChild(r);
    return r;
  }
  const el = (id) => { root(); return document.getElementById(id); };

  function setCursor(x, y) {
    state.x = x; state.y = y;
    const c = el("__dh_cursor");
    c.style.display = state.visible ? "block" : "none";
    c.style.transform = `translate(${x - 3}px, ${y - 2}px)`;
  }

  const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);

  window.__dh = {
    showCursor(x, y) { state.visible = true; setCursor(x ?? state.x, y ?? state.y); },
    hideCursor() { state.visible = false; el("__dh_cursor").style.display = "none"; },
    pos() { return { x: state.x, y: state.y }; },
    moveCursor(x, y, ms) {
      state.visible = true;
      const sx = state.x < 0 ? x - 200 : state.x, sy = state.y < 0 ? y + 120 : state.y;
      // gentle arc for a natural motion
      const mx = (sx + x) / 2 + (y - sy) * 0.15, my = (sy + y) / 2 - (x - sx) * 0.15;
      const t0 = performance.now();
      return new Promise((resolve) => {
        function frame(now) {
          const p = Math.min(1, (now - t0) / Math.max(ms, 1)), e = ease(p);
          const ix = (1 - e) * (1 - e) * sx + 2 * (1 - e) * e * mx + e * e * x;
          const iy = (1 - e) * (1 - e) * sy + 2 * (1 - e) * e * my + e * e * y;
          setCursor(ix, iy);
          if (p < 1) requestAnimationFrame(frame); else resolve(true);
        }
        requestAnimationFrame(frame);
      });
    },
    ripple(x, y) {
      const r = document.createElement("div");
      r.style.cssText = `position:fixed;left:${x - 22}px;top:${y - 22}px;width:44px;height:44px;border-radius:50%;
        border:3px solid #ff9500;background:rgba(255,149,0,.25);transform:scale(.3);opacity:1;
        transition:transform .45s ease-out,opacity .6s ease-out;`;
      root().appendChild(r);
      requestAnimationFrame(() => { r.style.transform = "scale(1.4)"; r.style.opacity = "0"; });
      setTimeout(() => r.remove(), 700);
    },
    highlight(rect, label) {
      const h = el("__dh_hl"), pad = 6;
      h.style.left = rect.x - pad + "px"; h.style.top = rect.y - pad + "px";
      h.style.width = rect.width + pad * 2 + "px"; h.style.height = rect.height + pad * 2 + "px";
      h.style.display = "block";
      const l = el("__dh_label");
      if (label) {
        l.textContent = label; l.style.display = "block";
        l.style.left = Math.max(4, rect.x - pad) + "px";
        const above = rect.y - pad - 28;
        l.style.top = (above > 4 ? above : rect.y + rect.height + pad + 4) + "px";
      } else l.style.display = "none";
    },
    clearHighlight() { el("__dh_hl").style.display = "none"; el("__dh_label").style.display = "none"; },

    snapshot(limit) {
      limit = limit || 150;
      const sel = [
        "a[href]", "button", "input:not([type=hidden])", "select", "textarea", "summary",
        "[role=button]", "[role=link]", "[role=tab]", "[role=menuitem]", "[role=checkbox]", "[role=radio]",
        "[role=switch]", "[role=combobox]", "[role=option]", "[role=treeitem]", "[contenteditable=true]",
        "[onclick]", "[tabindex]:not([tabindex='-1'])",
      ].join(",");
      document.querySelectorAll("[data-dh-id]").forEach((n) => n.removeAttribute("data-dh-id"));
      const vw = innerWidth, vh = innerHeight, out = [];
      const clean = (s, n) => (s || "").replace(/\s+/g, " ").trim().slice(0, n || 80);
      const labelOf = (n) => {
        if (n.labels && n.labels.length) return clean(n.labels[0].innerText);
        const lb = n.getAttribute("aria-labelledby");
        if (lb) { const t = document.getElementById(lb.split(" ")[0]); if (t) return clean(t.innerText); }
        const wrap = n.closest("label"); if (wrap) return clean(wrap.innerText);
        return "";
      };
      let id = 0;
      for (const n of document.querySelectorAll(sel)) {
        if (n.closest("#" + ROOT_ID)) continue;
        const r = n.getBoundingClientRect();
        if (r.width < 2 || r.height < 2) continue;
        const cs = getComputedStyle(n);
        if (cs.visibility === "hidden" || cs.display === "none" || parseFloat(cs.opacity) === 0) continue;
        // skip elements nested inside an already-listed clickable with identical text
        id += 1;
        n.setAttribute("data-dh-id", String(id));
        const tag = n.tagName.toLowerCase();
        const item = {
          id, tag,
          role: n.getAttribute("role") || "",
          type: n.getAttribute("type") || "",
          text: clean(n.innerText || n.textContent),
          aria: clean(n.getAttribute("aria-label")),
          placeholder: clean(n.getAttribute("placeholder")),
          label: (tag === "input" || tag === "select" || tag === "textarea") ? labelOf(n) : "",
          name: n.getAttribute("name") || "",
          title: clean(n.getAttribute("title")),
          testid: n.getAttribute("data-testid") || n.getAttribute("data-test") || n.getAttribute("data-cy") || "",
          href: tag === "a" ? (n.getAttribute("href") || "").slice(0, 120) : "",
          value: (tag === "input" && n.type !== "password") || tag === "textarea" || tag === "select" ? clean(n.value, 40) : "",
          disabled: !!n.disabled,
          checked: n.type === "checkbox" || n.type === "radio" ? !!n.checked : undefined,
          in_view: r.bottom > 0 && r.right > 0 && r.top < vh && r.left < vw,
          box: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
        };
        if (tag === "select") item.options = Array.from(n.options).slice(0, 15).map((o) => clean(o.text, 30));
        out.push(item);
        if (out.length >= limit) break;
      }
      const headings = Array.from(document.querySelectorAll("h1,h2,h3,[role=heading]"))
        .map((h) => clean(h.innerText, 60)).filter(Boolean).slice(0, 20);
      const dialogs = Array.from(document.querySelectorAll("[role=dialog],[role=alertdialog],dialog[open]"))
        .map((d) => clean(d.innerText, 200)).slice(0, 3);
      const alerts = Array.from(document.querySelectorAll("[role=alert],[role=status],.toast,.message,.alert,.notification"))
        .map((d) => clean(d.innerText, 150)).filter(Boolean).slice(0, 5);
      return {
        url: location.href, title: document.title, headings, dialogs, alerts,
        text: clean(document.body ? document.body.innerText : "", 1500),
        elements: out, viewport: [vw, vh],
      };
    },
  };
  const boot = () => root();
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
