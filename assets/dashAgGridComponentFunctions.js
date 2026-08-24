// assets/dashAgGridComponentFunctions.js
// Clean registry for Dash AG Grid: cellRenderers + helper formatters.
// Works on Render (no ??, no optional chaining).

(function () {
  var w = window;

  // Registries used by Dash AG Grid
  var dagcomponentfuncs = (w.dashAgGridComponentFunctions =
    w.dashAgGridComponentFunctions || {});
  var dagfuncs = (w.dashAgGridFunctions = w.dashAgGridFunctions || {});

  // -----------------------------
  // Helpers
  // -----------------------------
  function getReact() {
    return w.React;
  }

  function toNum(v) {
    var n = Number(v);
    return isFinite(n) ? n : null;
  }

  function fmt2(v) {
    var n = toNum(v);
    return n === null ? "" : n.toFixed(2);
  }

  function fmtPct(v) {
    var n = toNum(v);
    if (n === null) return "";
    return (n >= 0 ? "+" : "") + n.toFixed(2) + "%";
  }

  function fmtVolCompactIN(v) {
    var n = toNum(v);
    if (n === null) return "";
    var a = Math.abs(n);
    if (a >= 1e7) return (n / 1e7).toFixed(2) + "Cr";
    if (a >= 1e5) return (n / 1e5).toFixed(2) + "L";
    if (a >= 1e3) return (n / 1e3).toFixed(2) + "K";
    return String(Math.round(n));
  }

  function tvUrlFor(sym) {
    var s = sym || "";
    // Special-case only what you requested
    var tvSym = s === "BAJAJ-AUTO" ? "BAJAJ_AUTO" : s;

    return (
      "https://www.tradingview.com/chart/?symbol=" +
      encodeURIComponent("NSE:" + tvSym) +
      "&interval=5"
    );
  }

  // Expose helpers for valueFormatter {"function": "..."} use-cases
  dagfuncs.toNum = toNum;
  dagfuncs.fmt2 = fmt2;
  dagfuncs.fmtPct = fmtPct;
  dagfuncs.fmtVolCompactIN = fmtVolCompactIN;

  // -----------------------------
  // Cell Renderers
  // -----------------------------

  // Symbol as TradingView link
  dagcomponentfuncs.SymbolCell = function (params) {
    var React = getReact();
    if (!React) return (params && params.value) ? params.value : "";

    var sym = (params && params.value) ? String(params.value) : "";
    var url = tvUrlFor(sym);

    return React.createElement(
      "a",
      {
        href: url,
        target: "_blank",
        rel: "noopener noreferrer",
        className: "stock-sym",
        onClick: function (e) { e.stopPropagation(); }
      },
      sym
    );
  };

  // Company name as TradingView link (uses Symbol from row)
  dagcomponentfuncs.CompanyLinkCell = function (params) {
    var React = getReact();
    if (!React) return (params && params.value) ? params.value : "";

    var name = (params && params.value) ? String(params.value) : "";
    var sym = (params && params.data && params.data.Symbol) ? String(params.data.Symbol) : "";
    var url = tvUrlFor(sym);

    return React.createElement(
      "a",
      {
        href: url,
        target: "_blank",
        rel: "noopener noreferrer",
        className: "company-link",
        onClick: function (e) { e.stopPropagation(); },
        title: sym ? (sym + " • " + name) : name
      },
      name
    );
  };

  // %Change pill
  dagcomponentfuncs.PctPill = function (params) {
    var React = getReact();
    if (!React) return (params && params.value) ? params.value : "";

    var v = toNum(params ? params.value : null);
    if (v === null) {
      return React.createElement("span", { className: "val-pill neutral" }, "—");
    }

    var cls = v > 0 ? "val-pill up" : (v < 0 ? "val-pill down" : "val-pill neutral");
    var arrow = v > 0 ? "▲ " : (v < 0 ? "▼ " : "• ");
    return React.createElement("span", { className: cls }, arrow + fmtPct(v));
  };

  // RFactor pill
  dagcomponentfuncs.RfactorPill = function (params) {
    var React = getReact();
    if (!React) return (params && params.value) ? params.value : "";

    var v = toNum(params ? params.value : null);
    if (v === null) {
      return React.createElement("span", { className: "val-pill rf neutral" }, "—");
    }
    return React.createElement("span", { className: "val-pill rf" }, fmt2(v) + "×");
  };

  // Volume pill
  dagcomponentfuncs.VolPill = function (params) {
    var React = getReact();
    if (!React) return (params && params.value) ? params.value : "";

    var v = toNum(params ? params.value : null);
    if (v === null) {
      return React.createElement("span", { className: "val-pill vol neutral" }, "—");
    }
    return React.createElement("span", { className: "val-pill vol" }, fmtVolCompactIN(v));
  };

  // Plain numeric cell (2 decimals)
  dagcomponentfuncs.Num2Cell = function (params) {
    var React = getReact();
    if (!React) return (params && params.value) ? params.value : "";

    var v = toNum(params ? params.value : null);
    if (v === null) return React.createElement("span", null, "—");
    return React.createElement("span", null, fmt2(v));
  };

  // Plain percent cell (signed, 2 decimals)
  dagcomponentfuncs.Pct2Cell = function (params) {
    var React = getReact();
    if (!React) return (params && params.value) ? params.value : "";

    var v = toNum(params ? params.value : null);
    if (v === null) return React.createElement("span", null, "—");
    return React.createElement("span", null, fmtPct(v));
  };

  // Burst cell: time normal + arrow colored
  // Value examples: "10:05↑", "11:20↓", "—"
  dagcomponentfuncs.BurstCell = function (params) {
    var React = getReact();
    if (!React) return (params && params.value) ? params.value : "—";

    var raw = (params && params.value != null) ? String(params.value) : "—";
    if (!raw || raw === "—") {
      return React.createElement("span", { className: "burst-wrap" }, "—");
    }

    var up = raw.indexOf("↑") >= 0;
    var down = raw.indexOf("↓") >= 0;

    var timeTxt = raw.replace("↑", "").replace("↓", "");
    var arrowTxt = up ? "↑" : (down ? "↓" : "");
    var arrowCls = up ? "burst-arrow-up" : (down ? "burst-arrow-down" : "burst-arrow");

    return React.createElement(
      "span",
      { className: "burst-wrap" },
      [
        React.createElement("span", { className: "burst-time", key: "t" }, timeTxt),
        React.createElement("span", { className: arrowCls, key: "a" }, arrowTxt)
      ]
    );
  };
})();