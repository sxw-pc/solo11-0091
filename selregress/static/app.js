(function () {
  "use strict";
  document.documentElement.classList.add("js-ready");

  document.addEventListener("keydown", function (event) {
    if (event.key !== "/" || event.ctrlKey || event.metaKey || event.altKey) {
      return;
    }
    var target = event.target;
    if (target && /^(INPUT|SELECT|TEXTAREA)$/.test(target.tagName)) {
      return;
    }
    var firstFilter = document.querySelector(".filters input");
    if (firstFilter) {
      event.preventDefault();
      firstFilter.focus();
    }
  });
})();
