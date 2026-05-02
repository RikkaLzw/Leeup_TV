(function () {
  function doubanProxy(url) {
    return `/image/douban?url=${encodeURIComponent(url)}`;
  }

  function normalizeUrl(url) {
    try {
      return new URL(url, window.location.origin).toString();
    } catch {
      return String(url || "");
    }
  }

  function uniqueUrls(urls) {
    const seen = new Set();
    return urls
      .filter(Boolean)
      .filter((url) => {
        const key = normalizeUrl(url);
        if (!key || seen.has(key)) return false;
        seen.add(key);
        return true;
      });
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

  function tryNextCandidate(image, candidates) {
    const tried = new Set((image.dataset.triedPosters || "").split("\n").filter(Boolean));
    tried.add(normalizeUrl(image.currentSrc || image.src));
    const next = uniqueUrls(candidates).find((candidate) => !tried.has(normalizeUrl(candidate)));
    if (!next) {
      image.dataset.triedPosters = Array.from(tried).join("\n");
      return false;
    }
    tried.add(normalizeUrl(next));
    image.dataset.triedPosters = Array.from(tried).join("\n");
    image.src = next;
    return true;
  }

  function retryDouban(image) {
    const rawPoster = image.dataset.rawPoster || "";
    if (!rawPoster) {
      retrySourcePoster(image);
      return;
    }
    const directCandidates = doubanHostCandidates(rawPoster);
    const proxyCandidates = directCandidates.map(doubanProxy);
    const proxyPoster = image.dataset.proxyPoster || "";
    if (tryNextCandidate(image, [...directCandidates, proxyPoster, ...proxyCandidates])) {
      return;
    }
    retrySourcePoster(image);
  }

  function retrySourcePoster(image) {
    const sourcePoster = image.dataset.sourcePoster || "";
    if (sourcePoster && tryNextCandidate(image, [sourcePoster])) {
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
