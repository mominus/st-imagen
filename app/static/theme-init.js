(function () {
  try {
    var isAdmin = document.title.indexOf("后台") >= 0;
    var key = isAdmin ? "st_imagen_admin_theme" : "theme";
    var theme = localStorage.getItem(key);
    if (theme !== "light" && theme !== "dark") {
      theme = isAdmin
        ? "light"
        : window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches
          ? "light"
          : "dark";
    }
    document.documentElement.setAttribute("data-theme", theme);
  } catch (_) {
    document.documentElement.setAttribute(
      "data-theme",
      document.title.indexOf("后台") >= 0 ? "light" : "dark"
    );
  }
})();
