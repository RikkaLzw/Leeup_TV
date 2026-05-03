(function () {
  document.addEventListener("click", (event) => {
    const button = event.target instanceof Element ? event.target.closest("[data-play-url]") : null;
    if (!(button instanceof HTMLElement)) return;
    const url = button.dataset.playUrl || "";
    if (url) window.location.assign(url);
  });
})();
