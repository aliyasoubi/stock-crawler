/* Pure helpers shared by the generated script and Node tests. */
const KapExportCore = (() => {
  const retryUntil = (header, now = Date.now()) => {
    const text = (header || "").trim();
    const delay = /^\d+$/.test(text) ? Number(text) * 1000 : Date.parse(text) - now;
    return now + Math.max(3600000, Number.isFinite(delay) ? delay : 0);
  };
  const isXlsx = bytes => bytes.length >= 100 && bytes[0] === 0x50 && bytes[1] === 0x4b && bytes[2] === 3 && bytes[3] === 4;
  const chunk = (items, size) => {
    if (!Number.isInteger(size) || size < 1 || size > 25) throw Error("batch size must be 1-25");
    return Array.from({length: Math.ceil(items.length / size)}, (_, i) => items.slice(i * size, (i + 1) * size));
  };
  return {retryUntil, isXlsx, chunk};
})();
if (typeof module !== "undefined" && module.exports) module.exports = KapExportCore;
