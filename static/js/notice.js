(function () {
  const openButton = document.getElementById("siteNoticeOpen");
  const modal = document.getElementById("siteNoticeModal");
  if (!openButton || !modal) return;

  function openNotice() {
    modal.hidden = false;
    document.body.classList.add("modal-open");
    modal.querySelector("[data-notice-close]")?.focus?.();
  }

  function closeNotice() {
    modal.hidden = true;
    document.body.classList.remove("modal-open");
    openButton.focus();
  }

  openButton.addEventListener("click", openNotice);
  modal.addEventListener("click", (event) => {
    if (event.target.closest("[data-notice-close]")) closeNotice();
  });
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !modal.hidden) closeNotice();
  });
})();
