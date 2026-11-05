/* Foolscap site runtime: theme toggle + token-driven Observable Plot defaults. */

(function () {
  const root = document.documentElement;
  const stored = localStorage.getItem("fc-theme");
  const preferred =
    stored || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  if (preferred === "dark") root.setAttribute("data-theme", "dark");

  window.fcToggleTheme = function () {
    const dark = root.getAttribute("data-theme") === "dark";
    if (dark) root.removeAttribute("data-theme");
    else root.setAttribute("data-theme", "dark");
    localStorage.setItem("fc-theme", dark ? "light" : "dark");
    document.dispatchEvent(new CustomEvent("fc:theme"));
  };
})();

/* Read the current token values off the DOM so charts re-theme live. */
function fcTokens() {
  const s = getComputedStyle(document.documentElement);
  const v = (name) => s.getPropertyValue(name).trim();
  return {
    // chart.1–6 is the core ramp; 7–12 extend it for figures with more categories
    series: Array.from({ length: 12 }, (_, i) => v(`--fc-color-chart-${i + 1}`)).filter(Boolean),
    ink: v("--fc-color-chart-ink"),
    grid: v("--fc-color-chart-grid"),
    axis: v("--fc-color-chart-axis"),
    mono: v("--fc-font-family-data"),
    highlight: v("--fc-color-emphasis-highlight"),
  };
}

/* Default Plot options for the Foolscap figure style. */
function fcPlotDefaults(overrides = {}) {
  const t = fcTokens();
  return Object.assign(
    {
      style: {
        background: "transparent",
        color: t.axis,
        fontFamily: t.mono,
        fontSize: "11px",
      },
      grid: true,
      color: { range: t.series },
      ...{},
    },
    overrides
  );
}

/* Render a chart into `el`, re-rendering on theme change and when `el` is resized.
   `build` is a function(tokens) -> Plot spec (the object passed to Plot.plot).
   Options:
     onRender(plot, tokens)  called after each render with the element Plot returned
                             (for scale-aware overlays and pointer handling).
   The first render waits until `el` has a width, so charts can be built into
   detached nodes. Listeners detach themselves once `el` leaves the document;
   call the returned dispose() to detach them sooner. */
function fcChart(el, build, opts = {}) {
  let lastWidth = -1;
  let rendered = false;
  const render = () => {
    el.innerHTML = "";
    const t = fcTokens();
    const spec = build(t);
    if (!("width" in spec)) spec.width = Math.max(320, el.clientWidth || 640);
    // apply foolscap chart furniture unless the spec overrides it
    spec.style = Object.assign(
      { background: "transparent", color: t.axis, fontFamily: t.mono, fontSize: "11px" },
      spec.style || {}
    );
    if (!("color" in spec)) spec.color = { range: t.series };
    for (const ax of ["x", "y"]) {
      spec[ax] = Object.assign({ grid: true }, spec[ax] || {});
    }
    const plot = Plot.plot(spec);
    el.append(plot);
    // paint grid lines in the graph-paper ink
    el.querySelectorAll("[aria-label$='grid'] line").forEach((l) => {
      l.setAttribute("stroke", t.grid);
      l.setAttribute("stroke-opacity", "1");
    });
    lastWidth = el.clientWidth;
    rendered = true;
    if (opts.onRender) opts.onRender(plot, t);
  };
  const onTheme = () => (el.isConnected ? render() : dispose());
  let ro = null;
  const onResize = () => {
    clearTimeout(el._fcT);
    el._fcT = setTimeout(() => {
      if (!el.isConnected) return rendered && dispose();
      if (el.clientWidth && el.clientWidth !== lastWidth) render();
    }, rendered ? 150 : 0);
  };
  function dispose() {
    document.removeEventListener("fc:theme", onTheme);
    if (ro) ro.disconnect();
    else removeEventListener("resize", onResize);
    clearTimeout(el._fcT);
  }
  if (el.isConnected && el.clientWidth) render();
  document.addEventListener("fc:theme", onTheme);
  if (typeof ResizeObserver === "function") {
    ro = new ResizeObserver(onResize);
    ro.observe(el);
  } else {
    if (!rendered) render();
    addEventListener("resize", onResize);
  }
  return { render, dispose };
}
