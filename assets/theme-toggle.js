(() => {
  const KEY = "tt-theme";
  const mqMobile = window.matchMedia("(max-width: 980px)");

  const apply = (mode) => {
    const root = document.documentElement;
    if (mode === "light") root.setAttribute("data-theme", "light");
    else root.removeAttribute("data-theme"); // dark default
  };

  const preferredDesktopTheme = () => {
    const saved = localStorage.getItem(KEY);
    if (saved === "light" || saved === "dark") return saved;
    const prefersLight = window.matchMedia?.("(prefers-color-scheme: light)")?.matches;
    return prefersLight ? "light" : "dark";
  };

  const applyByContext = () => {
    if (mqMobile.matches) {
      // ALWAYS dark on mobile
      apply("dark");
      return;
    }
    apply(preferredDesktopTheme());
  };

  // Apply immediately
  applyByContext();

  const bind = () => {
    // On mobile we hide button and force dark; no need to bind
    if (mqMobile.matches) return true;

    const btn = document.getElementById("themeToggle");
    if (!btn) return false;
    if (btn.dataset.ttBound === "1") return true;
    btn.dataset.ttBound = "1";

    btn.addEventListener("click", () => {
      if (mqMobile.matches) {
        apply("dark");
        return;
      }
      const isLight = document.documentElement.getAttribute("data-theme") === "light";
      const next = isLight ? "dark" : "light";
      apply(next);
      localStorage.setItem(KEY, next);
    });

    return true;
  };

  // Dash async render: retry bind
  const retry = () => {
    if (bind()) return;
    setTimeout(retry, 250);
  };
  retry();

  // Re-bind after Dash renders
  document.addEventListener("dash:app-rendered", () => {
    applyByContext();
    bind();
  });

  // If screen size changes (rotate / resize), re-apply rule
  mqMobile.addEventListener?.("change", () => {
    applyByContext();
  });
})();