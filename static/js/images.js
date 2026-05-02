(function () {
  function doubanProxy(url) {
    return `/image/douban?url=${encodeURIComponent(url)}`;
  }

  function doubanHostCandidates(url) {
    let parsed;
    try {
      parsed = new URL(url, window.location.origin);
    } catch {
      return [];
    }
    const hosts = [parsed.hostname, "img1.doubanio.com", "img2.doubanio.com", "img3.doubanio.com"];
    const seen = new Set();
    return hosts
      .filter((host) => host && host.includes("doubanio.com"))
      .map((host) => {
        parsed.hostname = host;
        return parsed.toString();
      })
      .filter((candidate) => {
        if (seen.has(candidate)) return false;
        seen.add(candidate);
        return true;
      });
  }

  function retryDouban(image) {
    const rawPoster = image.dataset.rawPoster || "";
    if (!rawPoster) {
      retrySourcePoster(image);
      return;
    }
    const candidates = doubanHostCandidates(rawPoster).map(doubanProxy);
    const index = Number(image.dataset.retryIndex || 0);
    const next = candidates[index + 1];
    if (next) {
      image.dataset.retryIndex = String(index + 1);
      image.src = next;
      return;
    }
    retrySourcePoster(image);
  }

  function retrySourcePoster(image) {
    const sourcePoster = image.dataset.sourcePoster || "";
    if (sourcePoster && image.src !== sourcePoster && image.dataset.triedSource !== "1") {
      image.dataset.triedSource = "1";
      image.src = sourcePoster;
      return;
    }
    image.remove();
    image.parentElement?.classList.add("poster-missing");
  }

  function retryPoster(image) {
    if (image.dataset.rawPoster) {
      retryDouban(image);
      return;
    }
    retrySourcePoster(image);
  }

  window.RikkaImages = { retryDouban, retryPoster };
})();
