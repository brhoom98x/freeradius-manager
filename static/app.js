/* Progressive enhancement only. Every form here submits correctly with
   JavaScript disabled; this file just hides fields that are not currently
   relevant and remembers the theme choice. */
(function () {
  "use strict";

  /* --- theme ------------------------------------------------------------ */
  // The <head> applies the stored theme before first paint; this only wires up
  // the toggle and keeps its label in sync.
  var MODES = ["system", "light", "dark"];

  function storedTheme() {
    try {
      var t = localStorage.getItem("theme");
      return MODES.indexOf(t) === -1 ? "system" : t;
    } catch (e) {
      return "system";
    }
  }

  function applyTheme(mode) {
    if (mode === "system") {
      document.documentElement.removeAttribute("data-theme");
    } else {
      document.documentElement.setAttribute("data-theme", mode);
    }
    try {
      localStorage.setItem("theme", mode);
    } catch (e) {
      /* private window: the choice just will not persist */
    }
    document.querySelectorAll("[data-theme-btn]").forEach(function (btn) {
      var isCurrent = btn.getAttribute("data-theme-btn") === mode;
      btn.classList.toggle("is-active", isCurrent);
      btn.setAttribute("aria-pressed", isCurrent ? "true" : "false");
    });
  }

  document.querySelectorAll("[data-theme-btn]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      applyTheme(btn.getAttribute("data-theme-btn"));
    });
  });
  applyTheme(storedTheme());

  /* --- Custom… dropdowns ------------------------------------------------ */
  function syncPreset(select) {
    var target = document.getElementById(select.getAttribute("data-custom"));
    if (!target) return;
    var custom = select.value === "custom";
    target.hidden = !custom;
    target.querySelectorAll("input").forEach(function (input) {
      // an empty required number would block submit while hidden
      input.disabled = !custom;
    });
  }

  document.querySelectorAll("select.js-preset").forEach(function (select) {
    syncPreset(select);
    select.addEventListener("change", function () {
      syncPreset(select);
    });
  });

  /* --- simple / advanced rate picker ------------------------------------ */
  document.querySelectorAll(".rate-picker").forEach(function (picker) {
    var mode = picker.querySelector(".js-mode");

    function setMode(next) {
      picker.setAttribute("data-mode", next);
      mode.value = next;
      // whichever half is inactive must not submit its values
      picker.querySelectorAll(".rate-simple select, .rate-simple input")
        .forEach(function (el) { el.disabled = next !== "simple"; });
      picker.querySelectorAll(".rate-advanced input")
        .forEach(function (el) { el.disabled = next !== "advanced"; });
      // re-apply the Custom… rule, which setMode above has just overridden
      if (next === "simple") {
        picker.querySelectorAll("select.js-preset").forEach(syncPreset);
      }
      mode.disabled = false;
    }

    var toAdvanced = picker.querySelector(".js-to-advanced");
    var toSimple = picker.querySelector(".js-to-simple");
    if (toAdvanced) toAdvanced.addEventListener("click", function () { setMode("advanced"); });
    if (toSimple) toSimple.addEventListener("click", function () { setMode("simple"); });

    setMode(picker.getAttribute("data-mode") || "simple");
  });

  /* --- confirm destructive actions in-place ----------------------------- */
  document.querySelectorAll("[data-confirm]").forEach(function (el) {
    el.addEventListener("submit", function (event) {
      if (!window.confirm(el.getAttribute("data-confirm"))) event.preventDefault();
    });
  });
})();
