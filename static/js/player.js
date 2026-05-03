(function () {
  const cfg = window.PLAYER_CONFIG;
  let detail = cfg.detail;
  let currentEpisode = cfg.episode || 0;
  let currentSource = detail.source;
  let currentId = detail.id;
  let hls = null;
  let art = null;
  let loadTimer = null;
  let longPressTimer = null;
  let longPressActive = false;
  let longPressPointerId = null;
  let savedPlaybackRate = 1;
  let gestureStart = null;
  let activePlaybackKey = "";
  let introSkippedKey = "";
  let outroSkippedKey = "";
  let playerOptions = loadPlayerOptions();

  const playerContainer = document.getElementById("player");
  const playerOverlay = document.getElementById("playerOverlay");
  const playerOverlayText = document.getElementById("playerOverlayText");
  const playerLoadingHint = document.getElementById("playerLoadingHint");
  const playerGestureHint = document.getElementById("playerGestureHint");
  const preferStatus = document.getElementById("preferStatus");
  const candidateList = document.getElementById("candidateList");
  const episodeList = document.getElementById("episodeList");
  const episodeCount = document.getElementById("episodeCount");
  const speedTestButton = document.getElementById("speedTestButton");
  const mobileTabs = Array.from(document.querySelectorAll("[data-mobile-panel]"));
  const preferPanel = document.querySelector(".prefer-panel");
  const episodePanel = document.querySelector(".episode-panel");
  const resumeRecord = findResumeRecord();
  configureArtPlayer();
  const player = createPlayer();

  function configureArtPlayer() {
    if (!window.Artplayer) return;
    Artplayer.PLAYBACK_RATE = [0.5, 1, 1.25, 1.5, 2, 3];
    Artplayer.FAST_FORWARD_VALUE = 3;
    Artplayer.FAST_FORWARD_TIME = 700;
  }

  function createPlayer() {
    if (window.Artplayer && playerContainer) {
      art = new Artplayer({
        container: playerContainer,
        url: "",
        type: "",
        autoplay: false,
        setting: true,
        hotkey: true,
        pip: true,
        fullscreen: true,
        fullscreenWeb: true,
        playbackRate: true,
        gesture: true,
        fastForward: false,
        miniProgressBar: true,
        mutex: true,
        autoSize: false,
        autoPlayback: false,
        playsInline: true,
        settings: playerSettings(),
        moreVideoAttr: {
          controls: false,
          preload: "auto",
          playsInline: true
        },
        customType: {
          m3u8(video, url) {
            if (hls) {
              hls.destroy();
              hls = null;
            }
            if (window.Hls && Hls.isSupported()) {
              hls = new Hls({ enableWorker: true, fragLoadingTimeOut: 8000, manifestLoadingTimeOut: 8000 });
              hls.loadSource(url);
              hls.attachMedia(video);
              hls.on(Hls.Events.MANIFEST_PARSED, (_event, data) => {
                resumeTime(video.__rikkaResumeTime || 0);
                maybeSkipOpening();
                clearLoadTimer();
                setPlayerOverlay("", false);
              });
              hls.on(Hls.Events.ERROR, (_event, data) => {
                if (data?.fatal) {
                  if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
                    hls.startLoad();
                  } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
                    hls.recoverMediaError();
                  } else {
                    setPlayerOverlay("当前源加载失败，可点击测速换源");
                  }
                }
              });
            } else if (video.canPlayType("application/vnd.apple.mpegurl")) {
              video.src = url;
            }
          }
        }
      });
      return art.video;
    }

    const video = document.createElement("video");
    video.controls = true;
    video.playsInline = true;
    video.preload = "metadata";
    playerContainer?.appendChild(video);
    return video;
  }

  function setStatus(text) {
    if (preferStatus) preferStatus.textContent = text;
  }

  function setPlayerOverlay(text, visible = true) {
    const shouldShow = Boolean(visible && text);
    if (playerOverlay && playerOverlayText) {
      playerOverlayText.textContent = text;
      playerOverlay.hidden = !shouldShow;
    }
    if (playerLoadingHint) {
      playerLoadingHint.textContent = text || "";
      playerLoadingHint.hidden = !shouldShow;
    }
    if (art) {
      try {
        art.loading.show = shouldShow;
      } catch {
        // ArtPlayer loading state is best-effort across versions.
      }
      if (shouldShow) showPlayerNotice(text);
    }
  }

  function showPlayerNotice(text) {
    if (!text || !art) return;
    try {
      art.notice.show = text;
    } catch {
      // Ignore notice failures; the custom hint remains visible.
    }
  }

  function episodeTitle(index) {
    return String(index + 1);
  }

  function playUrl(url, resume) {
    if (!url) {
      setStatus("无播放地址");
      setPlayerOverlay("无播放地址");
      return;
    }
    setPlayerOverlay("正在加载视频");
    activePlaybackKey = `${currentSource}+${currentId}+${currentEpisode}+${Date.now()}`;
    introSkippedKey = "";
    outroSkippedKey = "";
    clearLoadTimer();
    loadTimer = window.setTimeout(() => {
      setPlayerOverlay("当前源加载较慢，可点击测速换源");
      tryNextTestedCandidate();
    }, 9000);
    const finalUrl = url;
    if (hls) {
      hls.destroy();
      hls = null;
    }
    if (art) {
      player.__rikkaResumeTime = resume || 0;
      art.type = /\.m3u8(\?|$)/i.test(finalUrl) ? "m3u8" : "";
      art.switchUrl(finalUrl);
      player.onloadedmetadata = () => {
        resumeTime(resume);
        maybeSkipOpening();
        clearLoadTimer();
        setPlayerOverlay("", false);
      };
    } else if (/\.m3u8(\?|$)/i.test(finalUrl) && window.Hls && Hls.isSupported()) {
      hls = new Hls({ enableWorker: true, fragLoadingTimeOut: 8000, manifestLoadingTimeOut: 8000 });
      hls.loadSource(finalUrl);
      hls.attachMedia(player);
      hls.on(Hls.Events.MANIFEST_PARSED, (_event, data) => {
        resumeTime(resume);
        maybeSkipOpening();
        clearLoadTimer();
        setPlayerOverlay("", false);
      });
      hls.on(Hls.Events.ERROR, (_event, data) => {
        if (data?.fatal) {
          if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
            hls.startLoad();
          } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
            hls.recoverMediaError();
          } else {
            setPlayerOverlay("当前源加载失败，可点击测速换源");
          }
        }
      });
    } else {
      player.src = finalUrl;
      player.onloadedmetadata = () => {
        resumeTime(resume);
        maybeSkipOpening();
        clearLoadTimer();
        setPlayerOverlay("", false);
      };
    }
  }

  function clearLoadTimer() {
    if (loadTimer) {
      window.clearTimeout(loadTimer);
      loadTimer = null;
    }
  }

  function resumeTime(resume) {
    if (resume && resume > 3 && Number.isFinite(player.duration)) {
      player.currentTime = Math.min(resume, Math.max(player.duration - 5, 0));
    }
  }

  function renderEpisodes() {
    if (!episodeList || !episodeCount) return;
    episodeList.innerHTML = "";
    episodeCount.textContent = `${detail.episodes.length} 集`;
    detail.episodes.forEach((url, index) => {
      const button = document.createElement("button");
      button.className = `episode-button${index === currentEpisode ? " active" : ""}`;
      button.textContent = episodeTitle(index);
      button.addEventListener("click", () => {
        currentEpisode = index;
        renderEpisodes();
        playUrl(detail.episodes[currentEpisode], 0);
      });
      episodeList.appendChild(button);
    });
  }

  function setMobilePanel(panel) {
    if (!episodePanel || !mobileTabs.length) {
      preferPanel?.classList.remove("mobile-hidden");
      return;
    }
    const isSources = panel === "sources";
    episodePanel?.classList.toggle("mobile-hidden", isSources);
    preferPanel?.classList.toggle("mobile-hidden", !isSources);
    mobileTabs.forEach((tab) => {
      const active = tab.dataset.mobilePanel === panel;
      tab.classList.toggle("active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
    });
  }

  function isMobileViewport() {
    return window.matchMedia("(max-width: 860px)").matches;
  }

  function renderCandidates(candidates) {
    const list = Array.from(candidates || []);
    candidateList.__candidates = list;
    candidateList.innerHTML = "";
    if (!list.length) {
      const empty = document.createElement("div");
      empty.className = "candidate-empty";
      empty.textContent = cfg.preferEnabled ? "点击测速后显示候选源" : "当前为直连播放";
      candidateList.appendChild(empty);
      return;
    }
    list.forEach((candidate, candidateIndex) => {
      const test = candidate.test || {};
      const button = document.createElement("button");
      button.className = `candidate${candidate.source === currentSource && candidate.id === currentId ? " active" : ""}`;
      button.dataset.index = String(candidateIndex);
      button.dataset.key = `${candidate.source}+${candidate.id}`;
      button.innerHTML = `
        <span class="candidate-title">${escapeHtml(candidate.source_name || candidate.source)}</span>
        ${renderCandidateMetrics(test)}
      `;
      button.addEventListener("click", () => {
        applyCandidate(candidate, false);
      });
      candidateList.appendChild(button);
    });
  }

  function ensureCandidateVisible(candidate) {
    requestAnimationFrame(() => {
      const active = candidateList.querySelector(
        `.candidate[data-key="${cssEscape(`${candidate.source}+${candidate.id}`)}"]`
      );
      active?.scrollIntoView({ block: "nearest" });
    });
  }

  function applyCandidate(candidate, autoPlay) {
    const handoffTime = currentPlaybackTime();
    if (handoffTime > 1) saveProgress();
    detail = candidate;
    currentSource = candidate.source;
    currentId = candidate.id;
    currentEpisode = Math.min(candidate.selected_episode || currentEpisode, detail.episodes.length - 1);
    document.getElementById("activeTitle").textContent = detail.title;
    document.getElementById("activeMeta").innerHTML = `<span>${escapeHtml(detail.source_name)}</span>${detail.year ? `<span>${escapeHtml(detail.year)}</span>` : ""}`;
    const desc = document.getElementById("activeDesc");
    if (desc) desc.textContent = detail.desc || "";
    renderEpisodes();
    renderCandidates(Array.from(candidateList.__candidates || []));
    ensureCandidateVisible(candidate);
    playUrl(detail.episodes[currentEpisode], resumeTimeFor(currentSource, currentId, currentEpisode, handoffTime));
    if (autoPlay) {
      playMedia();
    }
  }

  function initPlayback() {
    const metric = (cfg.sourceMetrics || {})[detail.source];
    renderCandidates([withMetricTest(detail, metric)]);
    if (cfg.preferEnabled) {
      setStatus(detail.recommended_by_metrics ? "站内推荐" : "手动测速");
    } else {
      setStatus("直连播放");
    }
    playUrl(detail.episodes[currentEpisode], resumeTimeFor(currentSource, currentId, currentEpisode));
  }

  function playMedia() {
    if (art) {
      const promise = art.play();
      if (promise?.catch) promise.catch(() => {});
      return;
    }
    player.play().catch(() => {});
  }

  function playNextEpisode() {
    const episodes = Array.from(detail.episodes || []);
    if (currentEpisode >= episodes.length - 1) return;
    saveProgress();
    currentEpisode += 1;
    renderEpisodes();
    playUrl(episodes[currentEpisode], 0);
    playMedia();
  }

  function playerSettings() {
    return [
      skipSetting("intro"),
      skipSetting("outro")
    ];
  }

  function skipSetting(kind) {
    const isIntro = kind === "intro";
    const enabledKey = isIntro ? "skipIntroEnabled" : "skipOutroEnabled";
    const secondsKey = isIntro ? "skipIntroSeconds" : "skipOutroSeconds";
    const title = isIntro ? "跳过片头" : "跳过片尾";
    return {
      width: 240,
      name: `skip-${kind}`,
      html: title,
      tooltip: skipTooltip(kind),
      selector: [
        {
          name: `skip-${kind}-enabled`,
          html: "启用",
          tooltip: playerOptions[enabledKey] ? "开" : "关",
          switch: Boolean(playerOptions[enabledKey]),
          onSwitch(item) {
            const next = !Boolean(item.switch);
            playerOptions[enabledKey] = next;
            item.tooltip = next ? "开" : "关";
            updateSkipParentTooltip(item, kind);
            savePlayerOptions();
            if (next && isIntro) maybeSkipOpening();
            return next;
          }
        },
        {
          width: 240,
          name: `skip-${kind}-seconds`,
          html: "时间",
          tooltip: `${playerOptions[secondsKey]}s`,
          range: [playerOptions[secondsKey], 0, 600, 5],
          onChange(item) {
            playerOptions[secondsKey] = normalizeSkipSeconds(item.range[0]);
            item.range = [playerOptions[secondsKey], 0, 600, 5];
            updateSkipParentTooltip(item, kind);
            savePlayerOptions();
            if (isIntro) maybeSkipOpening();
            return `${playerOptions[secondsKey]}s`;
          }
        }
      ]
    };
  }

  function skipTooltip(kind) {
    const isIntro = kind === "intro";
    const enabled = isIntro ? playerOptions.skipIntroEnabled : playerOptions.skipOutroEnabled;
    const seconds = isIntro ? playerOptions.skipIntroSeconds : playerOptions.skipOutroSeconds;
    return enabled ? `${seconds}s` : "关闭";
  }

  function updateSkipParentTooltip(item, kind) {
    if (item?.$parent) item.$parent.tooltip = skipTooltip(kind);
  }

  function loadPlayerOptions() {
    const defaults = {
      skipIntroEnabled: Boolean(cfg.playerOptions?.skipIntroEnabled),
      skipOutroEnabled: Boolean(cfg.playerOptions?.skipOutroEnabled),
      skipIntroSeconds: normalizeSkipSeconds(cfg.playerOptions?.skipIntroSeconds || 0),
      skipOutroSeconds: normalizeSkipSeconds(cfg.playerOptions?.skipOutroSeconds || 0)
    };
    try {
      const saved = JSON.parse(localStorage.getItem("mewkotv_player_options") || "{}");
      return {
        skipIntroEnabled: Boolean(saved.skipIntroEnabled ?? defaults.skipIntroEnabled),
        skipOutroEnabled: Boolean(saved.skipOutroEnabled ?? defaults.skipOutroEnabled),
        skipIntroSeconds: normalizeSkipSeconds(saved.skipIntroSeconds ?? defaults.skipIntroSeconds),
        skipOutroSeconds: normalizeSkipSeconds(saved.skipOutroSeconds ?? defaults.skipOutroSeconds)
      };
    } catch {
      return defaults;
    }
  }

  function savePlayerOptions() {
    try {
      localStorage.setItem("mewkotv_player_options", JSON.stringify(playerOptions));
    } catch {
      // ignore local storage failures
    }
  }

  function normalizeSkipSeconds(value) {
    return Math.max(0, Math.min(600, Math.round(Number(value || 0))));
  }

  function maybeSkipOpening() {
    if (!playerOptions.skipIntroEnabled) return;
    const seconds = normalizeSkipSeconds(playerOptions.skipIntroSeconds);
    if (!seconds || introSkippedKey === activePlaybackKey) return;
    if (!Number.isFinite(player.duration) || player.duration <= seconds + 3) return;
    if (player.currentTime > 3 || player.currentTime >= seconds) return;
    introSkippedKey = activePlaybackKey;
    player.currentTime = Math.min(seconds, Math.max(player.duration - 3, 0));
  }

  function maybeSkipEnding() {
    if (!playerOptions.skipOutroEnabled) return;
    const seconds = normalizeSkipSeconds(playerOptions.skipOutroSeconds);
    if (!seconds || outroSkippedKey === activePlaybackKey) return;
    if (!Number.isFinite(player.duration) || player.duration <= seconds + 3) return;
    const remaining = player.duration - player.currentTime;
    if (remaining > seconds || remaining <= 2) return;
    outroSkippedKey = activePlaybackKey;
    player.currentTime = Math.max(player.duration - 1, 0);
  }

  function setupLongPressFastForward() {
    if (!playerContainer) return;
    playerContainer.addEventListener("pointerdown", startLongPress);
    playerContainer.addEventListener("pointermove", moveLongPress, { passive: true });
    playerContainer.addEventListener("pointerup", stopLongPress, { passive: true });
    playerContainer.addEventListener("pointercancel", stopLongPress, { passive: true });
    playerContainer.addEventListener("pointerleave", stopLongPress, { passive: true });
    document.addEventListener("pointerup", stopLongPress, { passive: true });
    document.addEventListener("pointercancel", stopLongPress, { passive: true });
    window.addEventListener("blur", forceStopLongPress);
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) forceStopLongPress();
    });
  }

  function startLongPress(event) {
    if (event.pointerType === "mouse") return;
    if (!isMobileViewport() && event.pointerType !== "touch" && event.pointerType !== "pen") return;
    if (isPlayerControlTarget(event.target)) return;
    if (longPressActive) stopLongPress(event);
    longPressPointerId = event.pointerId;
    gestureStart = { x: event.clientX, y: event.clientY, pointerId: event.pointerId };
    clearLongPressTimer();
    longPressTimer = window.setTimeout(() => activateLongPress(), 450);
  }

  function moveLongPress(event) {
    if (longPressPointerId !== null && event.pointerId !== longPressPointerId) return;
    if (!gestureStart || longPressActive) return;
    const distance = Math.hypot(event.clientX - gestureStart.x, event.clientY - gestureStart.y);
    if (distance > 14) cancelPendingLongPress();
  }

  function activateLongPress() {
    if (!player) return;
    clearLongPressTimer();
    longPressActive = true;
    savedPlaybackRate = Number(player.playbackRate || 1);
    player.playbackRate = 3;
    showGestureHint("3x 快进中");
    if (player.paused) playMedia();
  }

  function stopLongPress(event) {
    if (event?.pointerId !== undefined && longPressPointerId !== null && event.pointerId !== longPressPointerId) {
      return;
    }
    clearLongPressTimer();
    gestureStart = null;
    longPressPointerId = null;
    if (!longPressActive) return;
    longPressActive = false;
    player.playbackRate = savedPlaybackRate || 1;
    hideGestureHint();
  }

  function forceStopLongPress() {
    stopLongPress();
  }

  function cancelPendingLongPress() {
    clearLongPressTimer();
    gestureStart = null;
    longPressPointerId = null;
  }

  function clearLongPressTimer() {
    if (!longPressTimer) return;
    window.clearTimeout(longPressTimer);
    longPressTimer = null;
  }

  function showGestureHint(text) {
    if (playerGestureHint) {
      playerGestureHint.textContent = text;
      playerGestureHint.hidden = false;
    }
    showPlayerNotice(text);
  }

  function hideGestureHint() {
    if (playerGestureHint) playerGestureHint.hidden = true;
  }

  function isPlayerControlTarget(target) {
    if (!(target instanceof Element)) return false;
    return Boolean(target.closest(
      ".art-bottom, .art-controls, .art-progress, .art-settings, .art-contextmenus"
    ));
  }

  function setupDesktopClickPlayback() {
    if (!playerContainer) return;
    playerContainer.addEventListener("click", (event) => {
      if (event.detail > 1 || isMobileViewport()) return;
      if (isPlayerControlTarget(event.target)) return;
      if (!(event.target instanceof Element)) return;
      if (event.target.closest(".art-video")) return;
      if (!event.target.closest(".art-video-player, .art-mask, .art-state, .art-poster")) return;
      togglePlayback();
    });
  }

  function togglePlayback() {
    if (!player) return;
    if (player.paused) {
      playMedia();
    } else {
      player.pause();
    }
  }

  async function runPreference() {
    if (!cfg.preferEnabled || speedTestButton?.disabled) return;
    const originalText = speedTestButton.textContent;
    speedTestButton.disabled = true;
    speedTestButton.textContent = "测速中";
    setStatus("正在全量测速");
    try {
      const res = await fetch("/api/prefer", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: (cfg.originalDetail || cfg.detail).title,
          source: (cfg.originalDetail || cfg.detail).source,
          id: (cfg.originalDetail || cfg.detail).id,
          episode: currentEpisode,
          year: (cfg.originalDetail || cfg.detail).year || "",
          kind: inferDetailKind(cfg.originalDetail || cfg.detail)
        })
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "优选失败");
      let candidates = data.candidates || [];
      candidateList.__candidates = candidates;
      renderCandidates(candidates);
      candidates = await testCandidatesInBrowser(candidates);
      const ranked = rankCandidates(candidates);
      candidateList.__candidates = ranked;
      renderCandidates(ranked);
      postSpeedResults(ranked);
      const best = ranked.find((candidate) => candidate.test?.ok);
      if (best) {
        setStatus(`测速完成：推荐 ${best.source_name}`);
        if (player.paused && player.currentTime < 1 && `${best.source}+${best.id}` !== `${currentSource}+${currentId}`) {
          applyCandidate(best, true);
        }
      } else {
        setStatus("没有可用候选源");
      }
    } catch (error) {
      setStatus(error.message || "测速失败");
    } finally {
      speedTestButton.disabled = false;
      speedTestButton.textContent = originalText || "测速";
    }
  }

  function inferDetailKind(item) {
    const typeName = String(item?.type_name || item?.class || "");
    if (/(电影|片|纪录|记录)/.test(typeName)) return "movie";
    if (/(剧|连续|综艺|动漫|番)/.test(typeName)) return "tv";
    const episodes = Array.isArray(item?.episodes) ? item.episodes.length : 0;
    if (episodes > 3) return "tv";
    if (episodes === 1) return "movie";
    return "";
  }

  async function testCandidatesInBrowser(candidates) {
    const list = Array.from(candidates || []);
    const batchSize = Math.max(1, Math.min(4, Number(cfg.speedTestConcurrency || 4)));
    const results = [];
    for (let start = 0; start < list.length; start += batchSize) {
      const batch = list.slice(start, start + batchSize);
      const tested = await Promise.all(batch.map(async (candidate) => {
        const selectedEpisode = Math.min(Math.max(currentEpisode, 0), Math.max((candidate.episodes || []).length - 1, 0));
        const url = candidate.selected_url || (candidate.episodes || [])[selectedEpisode] || "";
        const next = { ...candidate, selected_episode: selectedEpisode, selected_url: url };
        next.test = { ok: false, error_label: "测速中", quality: "未知", speed_label: "测速中", latency_ms: 0, speed_kbps: 0, score: 0 };
        updateCandidate(next);
        try {
          next.test = await browserMeasureStream(url);
        } catch {
          next.test = failTest();
        }
        updateCandidate(next);
        return next;
      }));
      results.push(...tested);
    }
    return results;
  }

  function browserMeasureStream(url) {
    if (!url) return Promise.resolve(failTest());
    if (/\.m3u8(\?|$)/i.test(url) && window.Hls && Hls.isSupported()) {
      return measureHlsStream(url);
    }
    return measureNativeStream(url);
  }

  function measureHlsStream(url) {
    return new Promise((resolve) => {
      const video = document.createElement("video");
      video.muted = true;
      video.preload = "auto";
      video.playsInline = true;
      video.style.cssText = "position:fixed;width:1px;height:1px;left:-9999px;top:-9999px;opacity:0;pointer-events:none;";
      document.body.appendChild(video);
      let hlsTester = null;
      let fragStart = 0;
      let latencyMs = 0;
      let speedKbps = 0;
      let manifestWidth = 0;
      let manifestHeight = 0;
      let resolved = false;
      let metadataReady = false;
      let speedReady = false;
      const pingStart = performance.now();
      fetch(url, { method: "HEAD", mode: "no-cors" })
        .then(() => { latencyMs = Math.round(performance.now() - pingStart); })
        .catch(() => { latencyMs = Math.round(performance.now() - pingStart); });

      const finish = (test) => {
        if (resolved) return;
        resolved = true;
        window.clearTimeout(timer);
        if (hlsTester) hlsTester.destroy();
        video.remove();
        resolve(test);
      };
      const maybeFinish = () => {
        if (!speedReady) return;
        const width = Number(video.videoWidth || manifestWidth || 0);
        const height = Number(video.videoHeight || manifestHeight || widthToHeight(width));
        finish(okTest(resolutionToQuality(width, height), height, latencyMs, speedKbps));
      };
      const timer = window.setTimeout(() => finish(failTest()), 9000);
      hlsTester = new Hls({ enableWorker: true, fragLoadingTimeOut: 5000, manifestLoadingTimeOut: 5000 });
      hlsTester.on(Hls.Events.MANIFEST_PARSED, (_event, data) => {
        const levels = Array.from(data?.levels || hlsTester?.levels || []);
        const bestLevel = levels
          .filter((level) => Number(level?.width || level?.height || 0) > 0)
          .sort((a, b) => Number(b.height || 0) - Number(a.height || 0))[0];
        if (bestLevel) {
          manifestWidth = Number(bestLevel.width || 0);
          manifestHeight = Number(bestLevel.height || 0);
        }
      });
      hlsTester.on(Hls.Events.FRAG_LOADING, () => {
        fragStart = performance.now();
      });
      hlsTester.on(Hls.Events.FRAG_LOADED, (_event, data) => {
        if (speedReady) return;
        const stats = data?.frag?.stats || data?.part?.stats || data?.stats || {};
        const loading = stats.loading || {};
        const loadStart = Number(loading.start || 0);
        const loadEnd = Number(loading.end || 0);
        const perfLoadTime = fragStart ? performance.now() - fragStart : 0;
        const statsLoadTime = loadStart > 0 && loadEnd > loadStart ? loadEnd - loadStart : 0;
        const loadTime = Math.max(perfLoadTime || statsLoadTime, 1);
        const payloadSize = Number(data?.payload?.byteLength || data?.payload?.length || 0);
        const size = Number(stats.loaded || stats.total || payloadSize || 0);
        if (size > 0) {
          speedKbps = (size / 1024) / (loadTime / 1000);
          if (loading.first && loadStart && !latencyMs) {
            latencyMs = Math.max(Math.round(Number(loading.first) - loadStart), 0);
          }
          speedReady = true;
          maybeFinish();
        }
      });
      hlsTester.on(Hls.Events.ERROR, (_event, data) => {
        if (data?.fatal) finish(failTest());
      });
      video.onloadedmetadata = () => {
        metadataReady = true;
        maybeFinish();
      };
      video.onerror = () => finish(failTest());
      hlsTester.loadSource(url);
      hlsTester.attachMedia(video);
    });
  }

  function measureNativeStream(url) {
    return new Promise((resolve) => {
      const started = performance.now();
      fetch(url, { method: "GET", headers: { Range: "bytes=0-524287" } })
        .then(async (response) => {
          const latencyMs = Math.round(performance.now() - started);
          const buffer = await response.arrayBuffer();
          const elapsed = Math.max(performance.now() - started, 1);
          const speedKbps = (buffer.byteLength / 1024) / (elapsed / 1000);
          resolve(okTest("未知", 0, latencyMs, speedKbps));
        })
        .catch(() => resolve(failTest()));
    });
  }

  function updateCandidate(candidate) {
    const list = Array.from(candidateList.__candidates || []);
    const key = `${candidate.source}+${candidate.id}`;
    const index = list.findIndex((item) => `${item.source}+${item.id}` === key);
    if (index >= 0) {
      list[index] = candidate;
      candidateList.__candidates = list;
      renderCandidates(list);
    }
  }

  function rankCandidates(candidates) {
    const list = Array.from(candidates || []);
    const okItems = list.filter((candidate) => candidate.test?.ok);
    const maxSpeed = Math.max(...okItems.map((candidate) => Number(candidate.test.speed_kbps || 0)), 1);
    const latencies = okItems.map((candidate) => Number(candidate.test.latency_ms || 0)).filter((value) => value > 0);
    const minLatency = latencies.length ? Math.min(...latencies) : 0;
    const maxLatency = latencies.length ? Math.max(...latencies) : 1;
    list.forEach((candidate) => {
      const test = candidate.test || {};
      if (!test.ok) {
        test.score = 0;
        return;
      }
      const speedScore = Math.min(100, (Number(test.speed_kbps || 0) / maxSpeed) * 100);
      const qualityScore = qualityScoreValue(test.quality);
      const latencyScore = maxLatency === minLatency
        ? 100
        : ((maxLatency - Number(test.latency_ms || maxLatency)) / (maxLatency - minLatency)) * 100;
      test.score = Math.round((speedScore * 0.7 + qualityScore * 0.2 + latencyScore * 0.1) * 100) / 100;
    });
    return list.sort((a, b) => Number(b.test?.score || 0) - Number(a.test?.score || 0));
  }

  function postSpeedResults(candidates) {
    fetch("/api/source-metrics", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ candidates })
    }).catch(() => {});
  }

  function tryNextTestedCandidate() {
    const currentKey = `${currentSource}+${currentId}`;
    const next = Array.from(candidateList.__candidates || [])
      .filter((candidate) => candidate.test?.ok)
      .find((candidate) => `${candidate.source}+${candidate.id}` !== currentKey);
    if (next) {
      setStatus(`当前源较慢，切换 ${next.source_name}`);
      applyCandidate(next, true);
    }
  }

  async function saveProgress() {
    if (!player.duration || player.currentTime < 1) return;
    const poster = pickRecordPoster();
    const record = {
      source: currentSource,
      id: currentId,
      title: detail.title,
      source_name: detail.source_name,
      year: detail.year,
      cover: poster.poster,
      poster: poster.poster,
      raw_poster: poster.raw_poster,
      source_poster: poster.source_poster,
      poster_source: poster.poster_source,
      episode_index: currentEpisode,
      total_episodes: detail.episodes.length,
      play_time: Math.floor(player.currentTime),
      total_time: Math.floor(player.duration),
      search_title: cfg.detail.title,
      save_time: Date.now()
    };
    saveLocalProgress(record);
  }

  function pickRecordPoster() {
    const items = [cfg.originalDetail, cfg.detail, detail].filter(Boolean);
    const doubanItem = items.find((item) => (
      item.raw_poster ||
      item.poster_source === "douban" ||
      String(item.poster || item.cover || "").includes("doubanio.com")
    ));
    const posterItem = doubanItem || items.find((item) => item.poster || item.cover) || {};
    const sourcePoster = posterItem.source_poster || items.map((item) => item.source_poster || "").find(Boolean) || "";
    const poster = posterItem.poster || posterItem.cover || sourcePoster || "";
    const rawPoster = posterItem.raw_poster || "";
    return {
      poster,
      raw_poster: rawPoster,
      source_poster: sourcePoster,
      poster_source: posterItem.poster_source || (rawPoster || poster.includes("doubanio.com") ? "douban" : "")
    };
  }

  function saveLocalProgress(record) {
    try {
      const key = "rikka_continue_records";
      const raw = localStorage.getItem(key);
      const records = raw ? JSON.parse(raw) : [];
      const filtered = Array.isArray(records)
        ? records.filter((item) => `${item.source}+${item.id}` !== `${record.source}+${record.id}`)
        : [];
      filtered.unshift(record);
      localStorage.setItem(key, JSON.stringify(filtered.slice(0, 30)));
    } catch {
      // ignore local storage failures
    }
  }

  function findResumeRecord() {
    const fallbackRecord = cfg.record || null;
    try {
      const raw = localStorage.getItem("rikka_continue_records");
      const records = raw ? JSON.parse(raw) : [];
      if (!Array.isArray(records)) return fallbackRecord;
      const original = cfg.originalDetail || cfg.detail || {};
      const originalKey = `${original.source || ""}+${original.id || ""}`;
      const activeKey = `${detail.source || ""}+${detail.id || ""}`;
      return records
        .filter((record) => record && Number(record.play_time || 0) > 3)
        .sort((a, b) => Number(b.save_time || 0) - Number(a.save_time || 0))
        .find((record) => {
          const key = `${record.source || ""}+${record.id || ""}`;
          return key === activeKey || key === originalKey || normalizeTitle(record.search_title || record.title) === normalizeTitle(original.title || detail.title);
        }) || fallbackRecord;
    } catch {
      return fallbackRecord;
    }
  }

  function currentPlaybackTime() {
    const value = Number(player?.currentTime || 0);
    return Number.isFinite(value) ? value : 0;
  }

  function resumeTimeFor(source, id, episode, handoffTime = 0) {
    if (handoffTime > 1) return handoffTime;
    if (!resumeRecord) return 0;
    const sameSource = `${resumeRecord.source || ""}+${resumeRecord.id || ""}` === `${source || ""}+${id || ""}`;
    const sameTitle = normalizeTitle(resumeRecord.search_title || resumeRecord.title) === normalizeTitle((cfg.originalDetail || cfg.detail || {}).title || detail.title);
    const sameEpisode = Number(resumeRecord.episode_index || 0) === Number(episode || 0);
    return (sameSource || sameTitle) && sameEpisode ? Number(resumeRecord.play_time || 0) : 0;
  }

  function normalizeTitle(value) {
    return String(value || "")
      .toLowerCase()
      .replace(/[\s\-_·:：,，.。!！?？()[\]（）【】]+/g, "");
  }

  function escapeHtml(value) {
    return String(value || "").replace(/[&<>"']/g, (char) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      "\"": "&quot;",
      "'": "&#039;"
    }[char]));
  }

  function renderCandidateMetrics(test) {
    if (test && test.kind === "metric") {
      if (!test.tests_total) {
        return '<span class="candidate-metrics">暂无源评分</span>';
      }
      const score = Number.isFinite(Number(test.source_score)) ? Number(test.source_score).toFixed(0) : "0";
      const rate = Number.isFinite(Number(test.success_rate)) ? `${Math.round(Number(test.success_rate) * 100)}%` : "0%";
      return `<span class="candidate-metrics">源评分 ${score} · 成功 ${rate} · ${Number(test.tests_total)} 次</span>`;
    }
    if (!test.ok) {
      const label = escapeHtml(test.error_label || "测速失败");
      return `<span class="candidate-metrics error"><span class="status-dot"></span>${label}</span>`;
    }
    const quality = escapeHtml(test.quality || "未知");
    const speed = escapeHtml(test.speed_label || "未知");
    const latency = Number.isFinite(Number(test.latency_ms)) ? `${Number(test.latency_ms)}ms` : "未知";
    const score = Number.isFinite(Number(test.score)) ? Number(test.score).toFixed(0) : "0";
    return `<span class="candidate-metrics">${quality} · ${speed} · ${latency} · ${score}</span>`;
  }

  function cssEscape(value) {
    if (window.CSS && CSS.escape) return CSS.escape(value);
    return String(value).replace(/["\\]/g, "\\$&");
  }

  function withMetricTest(candidate, metric) {
    return {
      ...candidate,
      selected_episode: currentEpisode,
      test: metric ? { kind: "metric", ...metric } : { kind: "metric", tests_total: 0 }
    };
  }

  function okTest(quality, height, latencyMs, speedKbps) {
    const cappedSpeedKbps = normalizedSpeedKbps(speedKbps);
    return {
      ok: true,
      error: "",
      error_label: "",
      quality: quality || "未知",
      height: height || 0,
      latency_ms: Math.max(Math.round(Number(latencyMs || 0)), 0),
      speed_kbps: cappedSpeedKbps,
      raw_speed_kbps: Math.round(Number(speedKbps || 0) * 10) / 10,
      speed_label: speedLabel(cappedSpeedKbps),
      score: 0,
      measured_by: "browser_hls"
    };
  }

  function failTest() {
    return {
      ok: false,
      error: "browser_probe_failed",
      error_label: "测速失败",
      quality: "未知",
      height: 0,
      latency_ms: 0,
      speed_kbps: 0,
      speed_label: "失败",
      score: 0,
      measured_by: "browser_hls"
    };
  }

  function speedLabel(speedKbps) {
    if (speedKbps >= 1024) return `${(speedKbps / 1024).toFixed(1)} MB/s`;
    return `${Math.round(speedKbps)} KB/s`;
  }

  function normalizedSpeedKbps(speedKbps) {
    const speed = Math.max(Number(speedKbps || 0), 0);
    const cap = Math.max(Number(cfg.browserSpeedCapKbps || 12288), 1024);
    return Math.round(Math.min(speed, cap) * 10) / 10;
  }

  function widthToQuality(width) {
    if (width >= 3840) return "4K";
    if (width >= 2560) return "2K";
    if (width >= 1920) return "1080p";
    if (width >= 1280) return "720p";
    if (width >= 854) return "480p";
    if (width > 0) return "SD";
    return "未知";
  }

  function resolutionToQuality(width, height) {
    const resolvedHeight = Number(height || 0);
    if (resolvedHeight >= 2160) return "4K";
    if (resolvedHeight >= 1440) return "2K";
    if (resolvedHeight >= 1080) return "1080p";
    if (resolvedHeight >= 720) return "720p";
    if (resolvedHeight >= 480) return "480p";
    return widthToQuality(Number(width || 0));
  }

  function widthToHeight(width) {
    if (width >= 3840) return 2160;
    if (width >= 2560) return 1440;
    if (width >= 1920) return 1080;
    if (width >= 1280) return 720;
    if (width >= 854) return 480;
    return 0;
  }

  function qualityScoreValue(quality) {
    if (quality === "4K") return 100;
    if (quality === "2K") return 85;
    if (quality === "1080p") return 75;
    if (quality === "720p") return 60;
    if (quality === "480p") return 40;
    if (quality === "SD") return 20;
    return 0;
  }

  speedTestButton?.addEventListener("click", runPreference);
  mobileTabs.forEach((tab) => {
    tab.addEventListener("click", () => setMobilePanel(tab.dataset.mobilePanel || "episodes"));
  });
  setupLongPressFastForward();
  setupDesktopClickPlayback();
  player?.addEventListener("pause", saveProgress);
  player?.addEventListener("loadstart", () => setPlayerOverlay("正在加载视频"));
  player?.addEventListener("loadedmetadata", () => {
    maybeSkipOpening();
  });
  player?.addEventListener("timeupdate", () => {
    maybeSkipEnding();
  });
  player?.addEventListener("playing", () => {
    clearLoadTimer();
    setPlayerOverlay("", false);
  });
  player?.addEventListener("canplay", () => {
    clearLoadTimer();
    setPlayerOverlay("", false);
  });
  player?.addEventListener("error", () => setPlayerOverlay("当前源加载失败，可点击测速换源"));
  if (art) {
    art.on("pause", saveProgress);
    art.on("video:playing", () => {
      clearLoadTimer();
      setPlayerOverlay("", false);
    });
    art.on("video:canplay", () => {
      clearLoadTimer();
      setPlayerOverlay("", false);
    });
    art.on("video:error", () => setPlayerOverlay("当前源加载失败，可点击测速换源"));
    art.on("video:ended", playNextEpisode);
  } else {
    player?.addEventListener("ended", playNextEpisode);
  }
  window.addEventListener("beforeunload", saveProgress);
  setInterval(saveProgress, 5000);

  renderEpisodes();
  setMobilePanel("episodes");
  initPlayback();
})();
