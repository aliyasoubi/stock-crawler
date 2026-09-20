/* Run only on the KAP English Financial Statement Item Search page. */
(async () => {
  if (location.origin !== "https://www.kap.org.tr" || location.pathname !== "/en/kalem-karsilastirma") {
    throw Error("Open https://www.kap.org.tr/en/kalem-karsilastirma first.");
  }
  if (!navigator.locks || !crypto.subtle) throw Error("A current browser with Web Locks and Web Crypto is required.");
  const config = KAP_EXPORT_CONFIG;
  const sha256 = async bytes => [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))].map(b => b.toString(16).padStart(2, "0")).join("");
  const encode = value => new TextEncoder().encode(JSON.stringify(value));
  const jobId = await sha256(encode({companies: config.companies, years: config.years, items: config.items, batchSize: config.batchSize}));
  const cooldownKey = "kap_export_host_stop_v6";
  const db = await new Promise((resolve, reject) => {
    const request = indexedDB.open("kap_export_v6", 1);
    request.onupgradeneeded = () => request.result.createObjectStore("batches", {keyPath: "key"});
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  const allSaved = () => new Promise((resolve, reject) => {
    const request = db.transaction("batches", "readonly").objectStore("batches").getAll();
    request.onsuccess = () => resolve(request.result.filter(b => b.jobId === jobId));
    request.onerror = () => reject(request.error);
  });
  const save = batch => new Promise((resolve, reject) => {
    const tx = db.transaction("batches", "readwrite");
    tx.objectStore("batches").put(batch);
    tx.oncomplete = resolve;
    tx.onerror = () => reject(tx.error);
    tx.onabort = () => reject(tx.error || Error("Storage transaction aborted"));
  });
  let stop = false;
  let unsaved = null;
  let active = false;
  const batches = KapExportCore.chunk(config.companies, config.batchSize);
  const toBase64 = bytes => {
    let binary = "";
    for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
    return btoa(binary);
  };
  window.kapStop = () => { stop = true; console.info("Stopping after the current request and durable save."); };
  window.kapDownloadSoFar = async () => {
    const stored = await allSaved();
    if (unsaved && !stored.some(b => b.key === unsaved.key)) stored.push(unsaved);
    const manifest = {schemaVersion: 6, source: "kap_compare", endpoint: "/en/api/export/compareItems", jobId,
      exportedAt: new Date().toISOString(), config, batches: stored.sort((a, b) => a.batchIndex - b.batchIndex)};
    const url = URL.createObjectURL(new Blob([JSON.stringify(manifest)], {type: "application/json"}));
    const a = document.createElement("a");
    a.href = url; a.download = `kap_manifest_${config.years.join("-")}_${jobId.slice(0, 12)}.json`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    console.info(`Downloaded ${stored.length}/${batches.length} batches. Keep this file; browser storage is not a backup.`);
  };
  window.kapProgressStatus = async () => console.info(`${(await allSaved()).length}/${batches.length} batches saved for ${jobId.slice(0, 12)}`);
  const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
  const run = async () => navigator.locks.request("kap-export-host-v6", {ifAvailable: true}, async lock => {
    if (!lock) throw Error("Another exporter tab is active; do not overlap runs.");
    const storedStop = JSON.parse(localStorage.getItem(cooldownKey) || "null");
    if (storedStop && (storedStop.blocked || storedStop.until > Date.now())) {
      throw Error(`KAP export is stopped: ${storedStop.reason}. Respect the source restriction before clearing ${cooldownKey}.`);
    }
    const saved = new Set((await allSaved()).map(b => b.key));
    let attempts = 0;
    for (let index = 0; index < batches.length; index++) {
      const companies = batches[index];
      const payload = {companyType: "IGS", mkkMemberIdList: companies.map(c => c.source_company_id),
        mkkMemberTitleList: companies.map(c => c.company_name), yearList: config.years.map(String), periodList: ["4"],
        itemIdList: config.items, sectors: ["GENERAL"]};
      const requestHash = await sha256(encode(payload));
      const key = `${jobId}:${requestHash}`;
      if (saved.has(key)) continue;
      if (stop || attempts >= config.maxRequests) break;
      // Delays reduce load, not a promise of access or protection from bans.
      await sleep(5000 + Math.random() * 5000);
      if (stop) break;
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 30000);
      attempts++;
      try {
        const response = await fetch("/en/api/export/compareItems", {method: "POST", credentials: "same-origin",
          redirect: "error", headers: {"Content-Type": "application/json", "Accept": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
          body: JSON.stringify(payload), signal: controller.signal});
        if (response.status === 429) {
          localStorage.setItem(cooldownKey, JSON.stringify({until: KapExportCore.retryUntil(response.headers.get("Retry-After")), reason: "HTTP 429"}));
          throw Error("HTTP 429: cooldown saved; no retries in this run.");
        }
        if ([401, 403].includes(response.status)) {
          localStorage.setItem(cooldownKey, JSON.stringify({blocked: true, reason: `HTTP ${response.status}`}));
          throw Error(`HTTP ${response.status}: access stopped.`);
        }
        if (response.status !== 200) throw Error(`HTTP ${response.status}: run stopped.`);
        const contentType = response.headers.get("Content-Type") || "";
        if (/html|json|xml(?!formats)/i.test(contentType) && !/spreadsheetml/.test(contentType)) {
          localStorage.setItem(cooldownKey, JSON.stringify({blocked: true, reason: `unexpected response type: ${contentType}`}));
          throw Error(`Unexpected response type: ${contentType}; access stopped for review.`);
        }
        const declaredSize = Number(response.headers.get("Content-Length") || 0);
        if (declaredSize > 20 * 1024 * 1024) throw Error("Workbook exceeds 20 MiB limit.");
        const bytes = new Uint8Array(await response.arrayBuffer());
        if (bytes.length > 20 * 1024 * 1024 || !KapExportCore.isXlsx(bytes)) {
          localStorage.setItem(cooldownKey, JSON.stringify({blocked: true, reason: "unexpected response; inspect for an access challenge"}));
          throw Error("Response is not an XLSX workbook; nothing marked complete.");
        }
        unsaved = {key, jobId, batchIndex: index, requestHash, payload, tickers: companies.map(c => c.ticker),
          fetchedAt: new Date().toISOString(), contentType, byteLength: bytes.length,
          sha256: await sha256(bytes), base64: toBase64(bytes)};
        await save(unsaved); // Quota/transaction failures are fatal and preserve an emergency download.
        unsaved = null;
        saved.add(key);
        console.info(`${saved.size}/${batches.length} saved; ${attempts}/${config.maxRequests} requests this run.`);
      } finally { clearTimeout(timer); }
    }
  });
  window.kapResume = async () => {
    if (active) throw Error("This exporter is already running.");
    active = true; stop = false;
    try { await run(); }
    catch (error) { console.error(error); }
    finally {
      active = false;
      console.info("Run ended. Use kapDownloadSoFar() to save results; kapResume() continues this same job after any cooldown.");
      if (unsaved) await window.kapDownloadSoFar();
      await window.kapProgressStatus();
    }
  };
  await window.kapResume();
})();
