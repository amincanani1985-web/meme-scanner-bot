(() => {
  const API = "https://api.dexscreener.com";
  const $ = (id) => document.getElementById(id);
  let busy = false;
  let lastSuccess = 0;
  let lastRows = [];

  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
  const num = (value) => {
    const n = Number(value);
    return Number.isFinite(n) ? n : 0;
  };
  const usd = (value) => {
    const n = num(value);
    if (!n) return "—";
    if (n >= 1000000) return "$" + (n / 1000000).toFixed(2) + "د";
    if (n >= 1000) return "$" + (n / 1000).toFixed(1) + "هزار";
    return "$" + n.toLocaleString("en-US", { maximumFractionDigits: 2 });
  };
  const price = (value) => {
    const n = num(value);
    if (!n) return "—";
    if (n >= 1) return "$" + n.toLocaleString("en-US", { maximumFractionDigits: 4 });
    return "$" + n.toPrecision(5);
  };
  const change = (value) => {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
    const n = Number(value);
    return (n > 0 ? "+" : "") + n.toFixed(2) + "%";
  };
  const age = (seconds) => {
    const ms = Date.now() - Number(seconds || 0) * 1000;
    if (!Number.isFinite(ms) || ms < 0) return "نامشخص";
    const minutes = Math.floor(ms / 60000);
    if (minutes < 1) return "همین الان";
    if (minutes < 60) return minutes + " دقیقه پیش";
    const hours = Math.floor(minutes / 60);
    if (hours < 48) return hours + " ساعت پیش";
    return Math.floor(hours / 24) + " روز پیش";
  };
  async function getJson(url) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 9000);
    try {
      const response = await fetch(url, { cache: "no-store", signal: controller.signal });
      if (!response.ok) throw new Error("HTTP " + response.status);
      return await response.json();
    } finally {
      clearTimeout(timer);
    }
  }
  function setStatus(text, state) {
    if ($("marketStatus")) $("marketStatus").textContent = text;
    if ($("marketDot")) $("marketDot").style.background =
      state === "ok" ? "var(--green)" : state === "warn" ? "var(--yellow)" : "var(--red)";
  }
  function paint(rows, note) {
    lastRows = rows;
    const gainers = rows.filter((r) => num(r.pair?.priceChange?.m5) > 0).length;
    const sellers = rows.filter((r) => {
      const tx = r.pair?.txns?.m5 || {};
      return num(tx.sells) > num(tx.buys);
    }).length;
    $("marketPairs").textContent = String(rows.length);
    $("marketGainers").textContent = String(gainers);
    $("marketSellers").textContent = String(sellers);
    $("marketUpdated").textContent = lastSuccess ? new Date(lastSuccess).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "—";
    if (!rows.length) {
      $("marketRows").innerHTML = '<tr><td colspan="7" class="live-empty">No current Solana pairs found for tracked candidates or latest token feeds.</td></tr>';
    } else {
      $("marketRows").innerHTML = rows.map((r) => {
        const p = r.pair || {};
        const tx = p.txns?.m5 || {};
        const ch = p.priceChange?.m5;
        const cls = num(ch) > 0 ? "up" : num(ch) < 0 ? "down" : "";
        const candidate = r.observation;
        const sub = candidate
          ? "نامزد بات · " + Number(candidate.hits || 1) + " بار دیده شده · آخرین مشاهده " + age(candidate.last_seen)
          : "کشف زنده بازار";
        const tokenUrl = p.pairAddress
          ? "https://dexscreener.com/solana/" + encodeURIComponent(p.pairAddress)
          : "https://dexscreener.com/solana/" + encodeURIComponent(r.mint);
        return '<tr><td><a class="token-link" href="' + esc(tokenUrl) + '" target="_blank" rel="noopener noreferrer">' +
          esc(p.baseToken?.symbol || r.profile?.symbol || (r.mint.slice(0, 5) + "…" + r.mint.slice(-4))) +
          '</a><span class="token-sub">' + esc(p.baseToken?.name || r.profile?.description || sub) +
          '</span><span class="token-sub">' + esc(sub) + '</span></td><td>' + price(p.priceUsd) +
          '</td><td class="' + cls + '">' + change(ch) + '</td><td>' + usd(p.liquidity?.usd) +
          '</td><td>' + usd(p.volume?.h1) + '</td><td>' + num(tx.buys) + ' / ' + num(tx.sells) +
          '</td><td>' + (candidate ? Number(candidate.last_score || 0).toFixed(1) + " / 100" : "—") +
          '</td></tr>';
      }).join("");
    }
    $("marketNote").textContent = note;
  }
  async function refreshMarketData() {
    if (busy) return;
    if (!window.LIVE_STATE) {
      setStatus("در انتظار داده‌های ثبت‌شده بات", "warn");
      $("marketNote").textContent = "قیمت‌های بازار مستقیماً دریافت می‌شوند، اما فهرست نامزدهای رصدشده بات هنوز بارگذاری نشده است.";
      return;
    }
    busy = true;
    setStatus("در حال به‌روزرسانی قیمت‌های زنده…", "warn");
    try {
      const [profilesResult, boostsResult] = await Promise.allSettled([
        getJson(API + "/token-profiles/latest/v1"),
        getJson(API + "/token-boosts/latest/v1")
      ]);
      const profiles = profilesResult.status === "fulfilled" && Array.isArray(profilesResult.value)
        ? profilesResult.value.filter((x) => x.chainId === "solana" && x.tokenAddress) : [];
      const boosts = boostsResult.status === "fulfilled" && Array.isArray(boostsResult.value)
        ? boostsResult.value.filter((x) => x.chainId === "solana" && x.tokenAddress) : [];
      const obs = window.LIVE_STATE.candidate_observations || {};
      const tracked = Object.entries(obs)
        .sort((a, b) => Number(b[1]?.last_seen || 0) - Number(a[1]?.last_seen || 0))
        .slice(0, 8)
        .map(([mint, observation]) => ({ mint, observation, source: "bot" }));
      const discoveries = [
        ...boosts.slice(0, 5).map((profile) => ({ mint: profile.tokenAddress, profile, source: "boost" })),
        ...profiles.slice(0, 4).map((profile) => ({ mint: profile.tokenAddress, profile, source: "profile" }))
      ];
      const byMint = new Map();
      for (const item of [...tracked, ...discoveries]) {
        if (!item.mint) continue;
        const existing = byMint.get(item.mint);
        if (!existing || item.observation) byMint.set(item.mint, { ...existing, ...item });
      }
      const candidates = [...byMint.values()].slice(0, 14);
      const results = await Promise.allSettled(candidates.map(async (item) => {
        const data = await getJson(API + "/latest/dex/tokens/" + encodeURIComponent(item.mint));
        const pairs = Array.isArray(data.pairs) ? data.pairs.filter((p) => p.chainId === "solana") : [];
        pairs.sort((a, b) => num(b.liquidity?.usd) - num(a.liquidity?.usd));
        return { ...item, pair: pairs[0] || null };
      }));
      const rows = results.filter((r) => r.status === "fulfilled" && r.value.pair).map((r) => r.value);
      rows.sort((a, b) => {
        if (a.observation && !b.observation) return -1;
        if (!a.observation && b.observation) return 1;
        return num(b.pair?.liquidity?.usd) - num(a.pair?.liquidity?.usd);
      });
      if (!rows.length && profilesResult.status === "rejected" && boostsResult.status === "rejected") {
        throw new Error("سرویس‌های DexScreener در دسترس نیستند");
      }
      lastSuccess = Date.now();
      paint(rows,  "داده زنده بازار از DexScreener · تازه‌سازی هر ۳۰ ثانیه. " +
        tracked.length +  " نامزد اخیر بات بررسی شد؛ " + discoveries.length +
        " مورد از پروفایل‌ها/توکن‌های تقویت‌شده اخیر بررسی شد. خود بات طبق زمان‌بندی اجرا می‌شود و به‌طور پیوسته اسکن نمی‌کند.");
      setStatus("بازار زنده · تازه‌سازی هر ۳۰ ثانیه", "ok");
    } catch (error) {
      if (lastRows.length) {
        paint(lastRows,  "تازه‌سازی بازار ناموفق بود؛ آخرین قیمت‌های موفق نمایش داده می‌شوند. " + String(error.message || error));
        setStatus("قیمت‌ها قدیمی‌اند · تلاش دوباره", "warn");
      } else {
        $("marketRows").innerHTML = '<tr><td colspan="7" class="live-empty live-error">Could not reach the live market API from this browser. Check connection or CORS/network restrictions; no live quote is being shown.</td></tr>';
        $("marketUpdated").textContent = "در دسترس نیست";
        setStatus("خوراک زنده در دسترس نیست", "error");
        $("marketNote").textContent = "درخواست رابط زنده ناموفق بود: " + String(error.message || error) +
          ". داده‌های ذخیره‌شده معاملات آزمایشی بات در بالا موجود است؛ این داده‌ها قیمت زنده بازار نیستند.";
      }
    } finally {
      busy = false;
    }
  }
  window.refreshMarketData = refreshMarketData;
  refreshMarketData();
  setInterval(refreshMarketData, 30000);
})();